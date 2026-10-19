"""model_router.py — 多模型路由层(第 13 期核心新增)

把"用哪个模型回答"从写死的一个常量,升级成"模型注册表 + 路由器":
简单任务自动路由到便宜模型,硬骨头自动路由到旗舰模型;
每次 LLM 调用都按 token × 单价记账落盘(成本 telemetry)。

[第 13 期] 本模块新增:
  - MODEL_REGISTRY     模型注册表(model_id → 档位/单价/强项/真实端点映射)
  - TIER_DEFAULT       自动路由时各档位(light/heavy)的默认模型
  - BACKGROUND_MODEL   后台任务(compact/子代理/队友)固定走的省钱模型
  - ROUTER_STATE       路由器运行时状态(模式/钉住模型/决策历史)
  - route_model        自动路由:按关键词/消息长度选档(规则可解释)
  - resolve_model      最终决策:manual 钉住优先,后台任务固定 light 档
  - llm_call           所有 LLM 调用的统一入口 = 路由 → 调用 → 记账
  - log_usage          成本记账:token × 单价 → usage_log.jsonl
  - 3 个工具实现       list_models / switch_model / cost_report(暴露给 LLM)
  - 2 个面板           print_model_panel / print_cost_panel(教学可视化)

路由一图流:

  用户消息 / 后台任务(compact / 子代理 / 队友)
       │
       ▼
  ┌─ resolve_model ──────────────────────────────┐
  │ manual 模式? → 用钉住的模型(switch_model 设的) │
  │ auto 模式?   → route_model(messages):         │
  │   ① 复杂关键词(审查/分析/设计/对比...)命中     │
  │      或消息很长 → heavy 档(deepseek-reasoner)  │
  │   ② 其余 → light 档(deepseek-chat,便宜)       │
  │ 后台任务 → 固定 BACKGROUND_MODEL(light 档)    │
  └──────────────────────────────────────────────┘
       │ model_id
       ▼
  llm_call:MODEL_REGISTRY[model_id]["api_model"] → 真实端点
       │
       ▼
  ┌─ log_usage(成本 telemetry)──────────────────┐
  │ token 数 × 注册表单价 → 美元成本             │
  │ 追加 usage_log.jsonl(时间/模型/任务/费用)    │
  └──────────────────────────────────────────────┘

设计要点:所有 LLM 调用必须过 llm_call 这一个入口 —— 路由在前(选模型),
记账在后(算成本)。位置决定职责,和第 12 期 Hooks 的挂载哲学一脉相承。
"""
import json
from datetime import datetime

from .config import client, MEMORY_DIR, USAGE_LOG_PATH


# ============ 模型注册表(本期核心) ============
# model_id → 档位 / 单价(美元 / 每百万 token,教学近似价)/ 强项 / api_model
#
# api_model = 真实发往端点的模型名。教学环境只有一个 DeepSeek key,所以
# claude-sonnet / local-qwen 用 DeepSeek 端点"模拟"—— 路由和计费机制与
# 生产完全一致。生产环境接入真实供应商只需改两处:
#   claude-sonnet:另建 OpenAI(base_url="https://api.anthropic.com/v1/", ...)
#   local-qwen:   另建 OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
# 注册表和 Agent 主逻辑一行都不用改 —— 这就是"路由层抽象供应商差异"的意义。
MODEL_REGISTRY: dict[str, dict] = {
    "deepseek-chat": {
        "tier": "light",                # 轻量档:便宜、快
        "input_price": 0.27,            # $/百万输入 token
        "output_price": 1.10,           # $/百万输出 token
        "strengths": "便宜、快。适合日常问答、简单工具调用、后台任务",
        "api_model": "deepseek-chat",   # 真实端点模型名
    },
    "deepseek-reasoner": {
        "tier": "heavy",                # 旗舰档:强、贵
        "input_price": 0.55,
        "output_price": 2.19,
        "strengths": "深度推理(思维链)。适合代码审查、复杂分析、技术决策",
        "api_model": "deepseek-reasoner",
    },
    "claude-sonnet": {
        "tier": "heavy",
        "input_price": 3.00,
        "output_price": 15.00,
        "strengths": "长文写作、复杂指令跟随(教学:DeepSeek 端点模拟计费)",
        "api_model": "deepseek-chat",   # 教学模拟;生产换 Anthropic base_url
    },
    "local-qwen": {
        "tier": "light",
        "input_price": 0.0,             # 本地部署:零 API 成本
        "output_price": 0.0,
        "strengths": "本地部署零成本、隐私友好(教学:DeepSeek 端点模拟)",
        "api_model": "deepseek-chat",   # 教学模拟;生产换 Ollama base_url
    },
}

# 自动路由时各档位的默认模型
TIER_DEFAULT = {
    "light": "deepseek-chat",
    "heavy": "deepseek-reasoner",
}

# 后台任务(compact 压缩 / 子代理 / 队友)固定走 light 档:
# 受限小任务不需要旗舰推理力 → 路由策略本身就是省钱策略
BACKGROUND_MODEL = "deepseek-chat"

# 路由器运行时状态(教学可视化 + 路由决策历史)
ROUTER_STATE: dict = {
    "mode": "auto",          # auto = 每轮自动路由 / manual = 钉住某模型
    "pinned_model": None,    # manual 模式下钉住的 model_id
    "current_model": None,   # 最近一次实际使用的 model_id
    "history": [],           # 路由决策历史 [{ts, from, to, reason, task}]
}

# 复杂任务关键词:命中即路由到 heavy 档(教学版规则,可解释、可自己加)
HEAVY_KEYWORDS = [
    "审查", "分析", "设计", "规划", "重构", "对比", "评估",
    "排查", "debug", "架构", "优化", "决策", "长文", "报告",
]

# 判定"长消息"的字符阈值(超长输入往往是硬任务)
HEAVY_LENGTH_THRESHOLD = 400


def route_model(messages: list[dict]) -> tuple[str, str]:
    """自动路由:按'最后一条用户消息'的任务特征选模型。

    规则(教学版,全部可解释):
    1. 命中复杂关键词(审查/分析/设计/对比...)→ heavy 档
    2. 消息超过 HEAVY_LENGTH_THRESHOLD 字符 → heavy 档
    3. 其余 → light 档(便宜)

    返回 (model_id, reason):reason 记入路由历史,教学可视化用。
    """
    # 找最后一条 user 消息(路由只看"当前要干什么")
    user_texts = [m["content"] for m in messages if m["role"] == "user" and m.get("content")]
    text = user_texts[-1] if user_texts else ""

    hits = [k for k in HEAVY_KEYWORDS if k in text.lower()]
    if hits:
        model_id = TIER_DEFAULT["heavy"]
        reason = f"命中复杂关键词 {hits[:3]}"
        return model_id, reason
    if len(text) > HEAVY_LENGTH_THRESHOLD:
        model_id = TIER_DEFAULT["heavy"]
        reason = f"消息超长({len(text)} 字符 > {HEAVY_LENGTH_THRESHOLD})"
        return model_id, reason
    model_id = TIER_DEFAULT["light"]
    reason = "简单任务(无复杂关键词,消息不长)"
    return model_id, reason


def resolve_model(messages: list[dict], task: str = "main") -> tuple[str, str]:
    """决定本次调用用哪个模型(manual 钉住优先,后台任务固定 light 档)。

    返回 (model_id, reason)。
    """
    if task != "main":
        # 后台任务(compact/子代理/队友):固定走便宜档 —— 路由策略即省钱策略
        return BACKGROUND_MODEL, f"后台任务({task})固定走 light 档省钱"
    if ROUTER_STATE["mode"] == "manual" and ROUTER_STATE["pinned_model"]:
        return ROUTER_STATE["pinned_model"], f"manual 模式:钉住 {ROUTER_STATE['pinned_model']}"
    return route_model(messages)


def _record_route(model_id: str, reason: str, task: str):
    """记录一次路由决策(更新 current_model + 追加历史)。"""
    ROUTER_STATE["current_model"] = model_id
    ROUTER_STATE["history"].append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "to": model_id,
        "reason": reason,
        "task": task,
    })
    tier = MODEL_REGISTRY[model_id]["tier"]
    in_p = MODEL_REGISTRY[model_id]["input_price"]
    out_p = MODEL_REGISTRY[model_id]["output_price"]
    print(f"  [路由] {reason} → {model_id}({tier}档, ${in_p}/${out_p} 每百万token)")


def llm_call(messages: list[dict], task: str = "main",
             tools: list[dict] | None = None, max_tokens: int = 4096):
    """第 13 期核心:所有 LLM 调用的统一入口 = 路由 → 调用 → 记账。

    与第 12 期直接 client.chat.completions.create(model=MODEL) 的区别:
    - 调用"前"过路由器:这次该用哪个模型(简单/复杂/钉住/后台)
    - 调用"后"过记账器:token 数 × 注册表单价 → 成本落盘 usage_log.jsonl

    返回 (response, model_id)。
    """
    # ① 路由:决定这次调用用哪个模型
    model_id, reason = resolve_model(messages, task=task)
    _record_route(model_id, reason, task)
    api_model = MODEL_REGISTRY[model_id]["api_model"]

    # ② 调用:按注册表映射发往真实端点(教学模拟:全部走 DeepSeek)
    kwargs: dict = {"model": api_model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    response = client.chat.completions.create(**kwargs)

    # ③ 记账:成本 telemetry(token × 单价 → 美元)
    log_usage(model_id, response.usage.prompt_tokens,
              response.usage.completion_tokens, task)
    return response, model_id


def log_usage(model_id: str, prompt_tokens: int, completion_tokens: int,
              task: str = "main") -> dict:
    """每次 LLM 调用记账:token 数 × 注册表单价 → 美元成本,追加 usage_log.jsonl。

    成本 = prompt_tokens / 1e6 × input_price + completion_tokens / 1e6 × output_price
    单价来自 MODEL_REGISTRY —— 换模型/调价只改注册表,记账逻辑零改动。
    """
    cfg = MODEL_REGISTRY[model_id]
    cost = (prompt_tokens / 1e6 * cfg["input_price"]
            + completion_tokens / 1e6 * cfg["output_price"])
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "model": model_id,
        "tier": cfg["tier"],
        "task": task,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": round(cost, 8),
    }
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with USAGE_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _load_usage_entries() -> list[dict]:
    """读取全部成本记账条目(坏行跳过)。"""
    if not USAGE_LOG_PATH.exists():
        return []
    entries = []
    with USAGE_LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


# ---- 多模型工具实现(暴露给 LLM 的 3 个) ----
def list_models() -> str:
    """查看模型注册表:每个模型的档位、单价、强项,以及当前路由状态。"""
    models = []
    for model_id, cfg in MODEL_REGISTRY.items():
        models.append({
            "model_id": model_id,
            "tier": cfg["tier"],
            "input_price_usd_per_m": cfg["input_price"],
            "output_price_usd_per_m": cfg["output_price"],
            "strengths": cfg["strengths"],
        })
    return json.dumps({
        "models": models,
        "router_mode": ROUTER_STATE["mode"],
        "pinned_model": ROUTER_STATE["pinned_model"],
        "current_model": ROUTER_STATE["current_model"],
        "tier_default": TIER_DEFAULT,
        "background_model": BACKGROUND_MODEL,
    }, ensure_ascii=False)


def switch_model(mode: str, model_id: str | None = None,
                 task: str | None = None) -> str:
    """切换路由模式:mode='auto' 恢复自动路由,mode='manual' 钉死指定模型。

    - auto + task='background':临时指定后台任务固定模型(默认已是 light 档)
    - manual + model_id:之后的每次 LLM 调用都固定用该模型
    """
    if mode == "auto":
        ROUTER_STATE["mode"] = "auto"
        ROUTER_STATE["pinned_model"] = None
        note = (f"已恢复自动路由:简单任务→{TIER_DEFAULT['light']},"
                f"复杂任务→{TIER_DEFAULT['heavy']}")
        if task == "background":
            note += f";后台任务仍固定 {BACKGROUND_MODEL}"
        print(f"  [路由] 切回 auto:简单→{TIER_DEFAULT['light']},复杂→{TIER_DEFAULT['heavy']}")
        return json.dumps({
            "switched": "auto",
            "mode": "auto",
            "note": note,
        }, ensure_ascii=False)

    # mode == "manual":必须带 model_id
    if not model_id:
        return json.dumps({
            "error": "manual 模式必须传 model_id",
            "available": list(MODEL_REGISTRY.keys()),
        }, ensure_ascii=False)

    if model_id not in MODEL_REGISTRY:
        return json.dumps({
            "error": f"未知模型: {model_id}",
            "available": list(MODEL_REGISTRY.keys()),
            "hint": "也可以传 mode='auto' 恢复自动路由",
        }, ensure_ascii=False)

    ROUTER_STATE["mode"] = "manual"
    ROUTER_STATE["pinned_model"] = model_id
    cfg = MODEL_REGISTRY[model_id]
    print(f"  [路由] manual 钉住 → {model_id}({cfg['tier']}档)")
    return json.dumps({
        "switched": model_id,
        "mode": "manual",
        "tier": cfg["tier"],
        "input_price_usd_per_m": cfg["input_price"],
        "output_price_usd_per_m": cfg["output_price"],
        "note": f"之后的对话将固定使用 {model_id},mode='auto' 可恢复自动路由",
    }, ensure_ascii=False)


def cost_report() -> str:
    """成本报表:按模型聚合 token 与费用,并对比'全部用最贵模型'的假想账单。"""
    entries = _load_usage_entries()
    if not entries:
        return json.dumps({
            "usage_log": [],
            "note": "成本日志还没有记录(还没有任何 LLM 调用)",
        }, ensure_ascii=False)

    by_model: dict[str, dict] = {}
    by_task: dict[str, dict] = {}
    for e in entries:
        m = e["model"]
        by_model.setdefault(m, {"calls": 0, "prompt_tokens": 0,
                                "completion_tokens": 0, "cost_usd": 0.0})
        by_model[m]["calls"] += 1
        by_model[m]["prompt_tokens"] += e["prompt_tokens"]
        by_model[m]["completion_tokens"] += e["completion_tokens"]
        by_model[m]["cost_usd"] = round(by_model[m]["cost_usd"] + e["cost_usd"], 8)

        t = e.get("task", "main")
        by_task.setdefault(t, {"calls": 0, "cost_usd": 0.0})
        by_task[t]["calls"] += 1
        by_task[t]["cost_usd"] = round(by_task[t]["cost_usd"] + e["cost_usd"], 8)

    total_cost = round(sum(v["cost_usd"] for v in by_model.values()), 8)

    # 假想账单:同样的 token 量,全部用注册表里"输出单价最贵"的模型
    priciest = max(MODEL_REGISTRY.items(), key=lambda kv: kv[1]["output_price"])
    priciest_id, priciest_cfg = priciest
    total_prompt = sum(v["prompt_tokens"] for v in by_model.values())
    total_completion = sum(v["completion_tokens"] for v in by_model.values())
    hypothetical = round(
        total_prompt / 1e6 * priciest_cfg["input_price"]
        + total_completion / 1e6 * priciest_cfg["output_price"], 8)

    return json.dumps({
        "total_calls": len(entries),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_cost_usd": total_cost,
        "by_model": by_model,
        "by_task": by_task,
        "hypothetical_all_priciest": {
            "model": priciest_id,
            "cost_usd": hypothetical,
        },
        "saved_usd": round(hypothetical - total_cost, 8),
        "note": f"实际花费 ${total_cost};若同样 token 全用最贵的 {priciest_id}"
                f"要 ${hypothetical},路由省下 ${round(hypothetical - total_cost, 8)}",
    }, ensure_ascii=False)


def print_model_panel():
    """打印多模型路由面板(教学可视化)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  🧭 Alex 模型路由面板(第 13 期新增)                    │")
    print("├" + "─" * 58 + "┤")
    mode = ROUTER_STATE["mode"]
    if mode == "auto":
        print(f"│  路由模式: auto(简单→{TIER_DEFAULT['light']},复杂→{TIER_DEFAULT['heavy']})")
    else:
        print(f"│  路由模式: manual(钉住 {ROUTER_STATE['pinned_model']})")
    print(f"│  当次使用: {ROUTER_STATE['current_model'] or '(还没有调用)'}")
    print("│" + "─" * 58 + "│")
    for model_id, cfg in MODEL_REGISTRY.items():
        cur = " ◀ 当前" if model_id == ROUTER_STATE["current_model"] else ""
        print(f"│  {model_id:18s} [{cfg['tier']:5s}] "
              f"${cfg['input_price']:.2f}/${cfg['output_price']:.2f} 每M{cur}")
    print("│" + "─" * 58 + "│")
    print("│  后台任务(compact/子代理/队友)固定:")
    print(f"│    {BACKGROUND_MODEL}(light 档,省钱策略)")
    if ROUTER_STATE["history"]:
        print("│" + "─" * 58 + "│")
        for h in ROUTER_STATE["history"][-3:]:
            line = f"  {h['ts'][11:]} → {h['to']}(task={h['task']})"
            print(f"│{line:<56}│")
    print("└" + "─" * 58 + "┘")
    print()


def print_cost_panel():
    """打印成本面板:按模型聚合的账单 + 省钱对比。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  💰 Alex 成本面板(第 13 期新增)                        │")
    print("├" + "─" * 58 + "┤")
    entries = _load_usage_entries()
    if not entries:
        print("│  成本日志为空(还没有任何 LLM 调用记账)")
        print("└" + "─" * 58 + "┘")
        return

    by_model: dict[str, dict] = {}
    for e in entries:
        m = e["model"]
        by_model.setdefault(m, {"calls": 0, "tok_in": 0, "tok_out": 0, "cost": 0.0})
        by_model[m]["calls"] += 1
        by_model[m]["tok_in"] += e["prompt_tokens"]
        by_model[m]["tok_out"] += e["completion_tokens"]
        by_model[m]["cost"] += e["cost_usd"]

    print(f"│  {'模型':18s} {'次数':>4s} {'tok_in':>7s} {'tok_out':>7s} {'成本$':>9s}")
    for m, v in by_model.items():
        print(f"│  {m:18s} {v['calls']:4d} {v['tok_in']:7d} "
              f"{v['tok_out']:7d} {v['cost']:9.6f}")

    total = sum(v["cost"] for v in by_model.values())
    priciest = max(MODEL_REGISTRY.items(), key=lambda kv: kv[1]["output_price"])
    total_in = sum(v["tok_in"] for v in by_model.values())
    total_out = sum(v["tok_out"] for v in by_model.values())
    hypo = (total_in / 1e6 * priciest[1]["input_price"]
            + total_out / 1e6 * priciest[1]["output_price"])

    print("│" + "─" * 58 + "│")
    print(f"│  实际成本: ${total:.6f}(共 {len(entries)} 次调用)")
    print(f"│  若全用最贵模型 {priciest[0]}: ${hypo:.6f}")
    print(f"│  路由省下: ${hypo - total:.6f}")
    print(f"│  (日志文件: {USAGE_LOG_PATH.name})")
    print("└" + "─" * 58 + "┘")
