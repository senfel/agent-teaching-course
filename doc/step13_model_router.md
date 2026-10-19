# 第 13 期:多模型路由(自动选模型 + 成本 telemetry)

## 问题

第 12 期结束,Alex 有了钩子安检、审计回放,工具总数 22 个。但回头看第 1 期就埋下的一个根子上的问题——**模型只有一个,还是写死的**:

```python
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
...
response = client.chat.completions.create(model=MODEL, ...)   # ← 12 期没动过这一行
```

想一个具体场景:用户问 Alex"现在几点了"。

- 这次调用只消耗 200 个 token,答案一秒出来
- 但如果答案是错的,用户看一眼手表就发现了——**错误的代价趋近于零**

再想另一个场景:用户说"分析一下这个架构方案,给个技术决策"。

- 需要多步推理,便宜的模型可能答得敷衍
- 错一次要人工返工——**错误的代价很高**

而第 12 期的 Alex 用**同一个模型**回答这两个问题。痛点清单:

- **成本浪费**——查时间也用旗舰模型,90% 的日常对话付着"深度推理"的钱。月底账单一拉,发现大半 token 花在"现在几点"上
- **质量错配**——反过来,真遇到硬任务,轻量模型答不好,用户觉得"这 Agent 不行"
- **供应商锁死**——想换 Claude 写长文、想加个本地模型保隐私?要改 client 初始化、改调用点,全代码搜索 `client.chat.completions.create` 一处处换
- **成本黑盒**——每个模型单价不同(deepseek-chat 输入 $0.27/M,claude-sonnet 输入 $3/M,差 11 倍),但 Agent 根本不知道自己花了多少钱、花在哪个模型上

本质问题:LLM 调用缺一个**统一入口**——在这个入口前面决定"这次用谁",在入口后面记账"这次花了多少"。这就是多模型路由(Multi-Model Routing)。

## 解决方案

### 1. 三个核心概念

| 概念 | 含义 | 解决什么问题 |
|---|---|---|
| 模型注册表(MODEL_REGISTRY) | model_id → 档位/单价/强项/真实端点映射 | 加模型 = 加一行注册,主逻辑零改动 |
| 路由器(route_model) | 按任务特征自动选模型:简单→light 档,复杂→heavy 档 | 该省省该花花,成本砍一个量级 |
| 成本 telemetry(log_usage) | 每次调用记 token × 单价 → 美元,追加 usage_log.jsonl | 账单可观测,出了事能对账 |

```
用户消息 / 后台任务(compact / 子代理 / 队友)
     │
     ▼
┌─ resolve_model(路由决策)──────────────────────┐
│ manual 模式? → 用钉住的模型(switch_model 设的) │
│ auto 模式?   → route_model(messages):          │
│   ① 命中复杂关键词(审查/分析/设计/对比...)      │
│      或消息超长 → heavy 档(deepseek-reasoner)   │
│   ② 其余 → light 档(deepseek-chat,便宜)        │
│ 后台任务 → 固定 BACKGROUND_MODEL(light 档)     │
└───────────────────────────────────────────────┘
     │ model_id
     ▼
llm_call:MODEL_REGISTRY[model_id]["api_model"] → 真实端点
     │
     ▼
┌─ log_usage(成本 telemetry)────────────────────┐
│ token 数 × 注册表单价 → 美元成本                │
│ 追加 usage_log.jsonl(时间/模型/任务/费用)      │
└───────────────────────────────────────────────┘
```

设计要点:**所有 LLM 调用必须过 llm_call 这一个入口**——路由在前(选模型),记账在后(算成本)。位置决定职责,和第 12 期 Hooks"挂在工具循环前后"是同一个哲学。

### 2. 模型注册表:一张表描述所有模型

```python
MODEL_REGISTRY: dict[str, dict] = {
    "deepseek-chat": {
        "tier": "light",              # 轻量档:便宜、快
        "input_price": 0.27,          # $/百万输入 token
        "output_price": 1.10,         # $/百万输出 token
        "strengths": "便宜、快。适合日常问答、简单工具调用、后台任务",
        "api_model": "deepseek-chat", # 真实发往端点的模型名
    },
    "deepseek-reasoner": {
        "tier": "heavy",              # 旗舰档:强、贵
        "input_price": 0.55, "output_price": 2.19,
        "strengths": "深度推理(思维链)。适合代码审查、复杂分析、技术决策",
        "api_model": "deepseek-reasoner",
    },
    "claude-sonnet": {
        "tier": "heavy",
        "input_price": 3.00, "output_price": 15.00,
        "strengths": "长文写作、复杂指令跟随(教学:DeepSeek 端点模拟计费)",
        "api_model": "deepseek-chat", # 教学模拟;生产换 Anthropic base_url
    },
    "local-qwen": {
        "tier": "light",
        "input_price": 0.0, "output_price": 0.0,   # 本地部署:零 API 成本
        "strengths": "本地部署零成本、隐私友好(教学:DeepSeek 端点模拟)",
        "api_model": "deepseek-chat", # 教学模拟;生产换 Ollama base_url
    },
}
```

注意 `api_model` 字段——**注册表里有两个身份**:对外叫 claude-sonnet(计费按 Claude 价),实际发往的端点可以映射成任何真实模型。教学环境只有一个 DeepSeek key,所以全部走 DeepSeek 端点"模拟";生产环境接入真实供应商只需为 claude 模型另建一个 `OpenAI(base_url="https://api.anthropic.com/v1/")` 客户端、为 local-qwen 另建 `OpenAI(base_url="http://localhost:11434/v1")`——**注册表和 Agent 主逻辑一行都不用改**。这就是"路由层抽象供应商差异"的意义。

### 3. 路由器:可解释的规则,不是黑盒

```python
HEAVY_KEYWORDS = [
    "审查", "分析", "设计", "规划", "重构", "对比", "评估",
    "排查", "debug", "架构", "优化", "决策", "长文", "报告",
]
HEAVY_LENGTH_THRESHOLD = 400          # 超长输入往往是硬任务


def route_model(messages) -> tuple[str, str]:
    """自动路由:按'最后一条用户消息'的任务特征选模型。"""
    user_texts = [m["content"] for m in messages if m["role"] == "user" and m.get("content")]
    text = user_texts[-1] if user_texts else ""

    hits = [k for k in HEAVY_KEYWORDS if k in text.lower()]
    if hits:
        return TIER_DEFAULT["heavy"], f"命中复杂关键词 {hits[:3]}"
    if len(text) > HEAVY_LENGTH_THRESHOLD:
        return TIER_DEFAULT["heavy"], f"消息超长({len(text)} 字符 > {HEAVY_LENGTH_THRESHOLD})"
    return TIER_DEFAULT["light"], "简单任务(无复杂关键词,消息不长)"
```

教学版规则简单到"一眼看懂、随手能改":关键词命中或消息超长 → heavy 档;其余 → light 档。**每条路由都带 reason**,记入历史面板——路由决策可解释、可调试。生产里可以把 route_model 换成"让 light 模型先分类再决定"(两级路由),但接口不变。

```python
def resolve_model(messages, task="main"):
    """决定本次调用用哪个模型(manual 钉住优先,后台任务固定 light 档)。"""
    if task != "main":
        # 后台任务(compact/子代理/队友):固定走便宜档 —— 路由策略即省钱策略
        return BACKGROUND_MODEL, f"后台任务({task})固定走 light 档省钱"
    if ROUTER_STATE["mode"] == "manual" and ROUTER_STATE["pinned_model"]:
        return ROUTER_STATE["pinned_model"], f"manual 模式:钉住 {ROUTER_STATE['pinned_model']}"
    return route_model(messages)
```

三层优先级:**manual 钉住 > auto 规则 > 后台任务固定档**。后台任务(compact 压缩、子代理、队友)永远走 light 档——它们是"无人直接消费结果"的受限小任务,用旗舰推理力纯属浪费,这是**路由策略本身就是省钱策略**的直接体现。

### 4. llm_call:唯一入口 = 路由 + 调用 + 记账

```python
def llm_call(messages, task="main", tools=None, max_tokens=4096):
    """第 13 期核心:所有 LLM 调用的统一入口 = 路由 → 调用 → 记账。"""
    # ① 路由:决定这次调用用哪个模型
    model_id, reason = resolve_model(messages, task=task)
    _record_route(model_id, reason, task)
    api_model = MODEL_REGISTRY[model_id]["api_model"]

    # ② 调用:按注册表映射发往真实端点
    kwargs = {"model": api_model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    response = client.chat.completions.create(**kwargs)

    # ③ 记账:成本 telemetry(token × 单价 → 美元)
    log_usage(model_id, response.usage.prompt_tokens,
              response.usage.completion_tokens, task)
    return response, model_id
```

改造前后对比,主对话循环只动了一行:

```python
# 第 12 期:
response = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS, ...)

# 第 13 期:
response, model_id = llm_call(messages, task="main", tools=TOOLS)
```

但全 Agent 有**四处** LLM 调用点,全部收编到 llm_call:

| 调用点 | task 标记 | 路由结果 |
|---|---|---|
| 主对话 chat_with_tools | `task="main"` | auto 规则(简单→light,复杂→heavy) |
| compact 压缩 compact_history | `task="compact"` | 固定 light 档 |
| 子代理 _run_subagent | `task="subagent"` | 固定 light 档 |
| 队友 _run_team_member | `task="team"` | 固定 light 档 |

**这正是"唯一入口"的价值**:一处新增路由/记账逻辑,四个调用点全部自动生效。如果不用统一入口,每处各写一遍,要么漏记账、要么换模型时四处改代码。

### 5. 成本 telemetry:token × 单价 → 美元

```python
def log_usage(model_id, prompt_tokens, completion_tokens, task="main"):
    """每次 LLM 调用记账,追加 usage_log.jsonl。"""
    cfg = MODEL_REGISTRY[model_id]
    cost = (prompt_tokens / 1e6 * cfg["input_price"]
            + completion_tokens / 1e6 * cfg["output_price"])
    entry = {
        "ts": ..., "model": model_id, "tier": cfg["tier"], "task": task,
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        "cost_usd": round(cost, 8),
    }
    with USAGE_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
```

**单价来自注册表**——换模型、调价格只改注册表数字,记账逻辑零改动。成本公式是行业通用的 `token/1e6 × 单价`,输入输出分开计费(claude-sonnet 输出 $15/M 是输入的 5 倍,压成本优先压输出)。

### 6. 三个新工具 + 两个新面板

| 新工具 | 作用 |
|---|---|
| `list_models` | 查看模型注册表(档位/单价/强项)+ 当前路由模式 + 最近路由决策 |
| `switch_model` | 切换路由模式:mode='auto' 恢复自动 / mode='manual'+model_id 钉死 |
| `cost_report` | 成本报表:按模型/按任务聚合 token 与费用,含"全用最贵模型"的假想账单对比 |

```
┌──────────────────────────────────────────────────────────┐
│  🧭 Alex 模型路由面板(第 13 期新增)                    │
├──────────────────────────────────────────────────────────┤
│  路由模式: auto(简单→deepseek-chat,复杂→deepseek-reasoner)
│  当次使用: deepseek-reasoner
│──────────────────────────────────────────────────────────│
│  deepseek-chat      [light] $0.27/$1.10 每M
│  deepseek-reasoner  [heavy] $0.55/$2.19 每M ◀ 当前
│  claude-sonnet      [heavy] $3.00/$15.00 每M
│  local-qwen         [light] $0.00/$0.00 每M
│──────────────────────────────────────────────────────────│
│  20:58:15 → deepseek-chat(task=main)
│  20:58:17 → deepseek-reasoner(task=main)   ← 命中"分析"
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  💰 Alex 成本面板(第 13 期新增)                        │
├──────────────────────────────────────────────────────────┤
│  模型                   次数  tok_in tok_out       成本$
│  deepseek-chat           6   33576     250  0.009341
│  deepseek-reasoner       6   35520    3846  0.027959
│──────────────────────────────────────────────────────────│
│  实际成本: $0.037299(共 12 次调用)
│  若全用最贵模型 claude-sonnet: $0.268728
│  路由省下: $0.231429
└──────────────────────────────────────────────────────────┘
```

成本面板的灵魂是最后三行:**"实际成本 vs 假想账单(全用最贵模型)"**。同样这批 token,全用 claude-sonnet 要 $0.269,路由实际花 $0.037——**省下 86%**。这一个数字就是多模型路由的全部卖点。

### 7. 演示场景:简单→light,复杂→heavy

```
你(自动演示 1): 现在几点了?顺便算一下 17 * 23

  [路由] 简单任务(无复杂关键词,消息不长) → deepseek-chat(light档, $0.27/$1.1 每百万token)
  [工具调用] get_current_time({})
  [工具调用] calculate({'expression': '17 * 23'})

[Alex] 现在:2026-10-16 20:58(周五,Asia/Shanghai);17 × 23 = 391

你(自动演示 2): 分析一下:Agent 什么时候该用便宜模型?+ 用 cost_report 汇报花费

  [路由] 命中复杂关键词 ['分析'] → deepseek-reasoner(heavy档, $0.55/$2.19 每百万token)
  [工具调用] list_models({})      ← 先查注册表
  [工具调用] cost_report({})      ← 再拉账单

[Alex] 总花费 $0.032,11 次调用;实际最贵的是 deepseek-reasoner($0.023,占 71%);
      若全用 claude-sonnet 同样 token 要 $0.239 —— 路由省下约 86%……
```

同一个会话里,两个问题被路由到两个档位的模型——**用户毫无感知,Alex 自己也毫无感知**(路由对调用方透明)。注意一个细节:cost_report 读到的是"调用那一刻"的账单,Alex 汇报完 $0.032 后,它自己这次回答又被记账,最终面板显示 $0.037——**telemetry 连"读报表"这次调用也记账**,这才是真账。

## 工作原理

### 多模型路由的三层结构:注册表层 + 路由层 + 挂载点

```
┌─────────────────────────────────────────────────────────┐
│  注册表层(MODEL_REGISTRY / TIER_DEFAULT / BACKGROUND)  │
│  4 个模型:档位/单价/强项/api_model 映射                 │
│  加模型 = 加一行注册;换供应商 = 改一个 api_model        │
├─────────────────────────────────────────────────────────┤
│  路由层(resolve_model → route_model / ROUTER_STATE)   │
│  manual 钉住 > auto 规则(关键词/长度)> 后台任务固定档 │
│  每次决策带 reason,历史可回放                           │
├─────────────────────────────────────────────────────────┤
│  挂载点(llm_call,全 Agent 唯一 LLM 入口)             │
│  路由(前) → client 调用 → log_usage 记账(后)        │
│  main/compact/subagent/team 四类任务全部收编            │
└─────────────────────────────────────────────────────────┘
```

| 层 | 职责 | 能做到 | 做不到 |
|---|---|---|---|
| 注册表 | 模型发现与定价 | 加模型零改主逻辑 | 运行时热更新(本期静态) |
| 路由层 | 模型选择 | auto/manual/后台三模式 | 按供应商分客户端(教学单端点) |
| 挂载点 | 策略生效范围 | 覆盖全部 4 类 LLM 调用 | 缓存/批处理等更高级省钱(下期) |

**关键洞察**:第 12 期 Hooks 解决"工具调用放不放行"(约束),本期路由解决"LLM 调用用谁+花多少"(资源分配)。两者结构同构——**都是"唯一入口 + 前置决策 + 后置记录"**,一个管安全,一个管成本。

### light / heavy / manual / background:四种路由路径完整对比

| 维度 | auto·简单 | auto·复杂 | manual 钉住 | background 后台 |
|---|---|---|---|---|
| 触发条件 | 无关键词且消息短 | 命中关键词/超长 | 用户说"用 XX 模型" | task≠main |
| 选中模型 | deepseek-chat | deepseek-reasoner | 任意注册模型 | deepseek-chat |
| 档位 | light | heavy | 看钉谁 | light |
| 典型场景 | 查时间/闲聊/算数 | 审查/分析/架构决策 | "接下来都用 claude 写" | compact/子代理/队友 |
| 谁说了算 | 路由规则 | 路由规则 | 用户 | 硬编码(省钱策略) |
| cost 差异 | $0.27/$1.10 每M | $0.55/$2.19 每M | 按注册表 | $0.27/$1.10 每M |

有人会问:后台任务为什么不也走 auto 规则?——compact/子代理/队友的任务描述是我们代码自己拼的(比如"请压缩以下对话历史"),不含用户原话,**auto 规则对它们失真**;而且它们的工作(摘要/受限执行)本来就不需要旗舰推理力。固定 light 档,既是正确,也是省钱。

### 一次 heavy 调用的完整流程

```
用户输入: "分析一下:Agent 什么时候该用便宜模型?"
    │
    ▼
chat_with_tools 循环 → llm_call(messages, task="main", tools=TOOLS)
    │
    ├─① resolve_model
    │     task == "main" → 走 auto
    │     route_model: 命中「分析」→ (deepseek-reasoner, "命中复杂关键词")
    │     _record_route: 面板历史 +1
    │
    ├─② client.chat.completions.create
    │     model = MODEL_REGISTRY["deepseek-reasoner"]["api_model"]
    │     → 真实发往端点(教学:DeepSeek;生产:任意注册端点)
    │
    ├─③ log_usage
    │     prompt_tokens=11824, completion_tokens=1101
    │     cost = 11824/1e6×0.55 + 1101/1e6×2.19 = $0.0089
    │     → usage_log.jsonl 追加一行
    │
    ▼
response 返回工具循环;下一轮"现在几点"→ 同一入口 → light 档
```

### 为什么 Agent 需要多模型路由?

| 没有(第 12 期) | 有(本期) |
|---|---|
| 查时间也用旗舰模型 | 简单任务自动走 $0.27/M 的 light 档 |
| 硬任务答得敷衍 | 命中"分析/审查"自动升级 reasoner |
| 换供应商改 N 处代码 | 改注册表一行,主逻辑零改动 |
| 账单是黑盒 | usage_log.jsonl 逐条记账,面板随时对账 |
| "省成本"只能拍脑袋 | 假想账单对比:同样 token 全用最贵要多花 86% |

本质上,多模型路由是给 Agent 装上**一台智能交换机**:每个请求按需分发给最合适的模型,并且每个包都计费。真实世界的企业级 Agent(LangGraph 的 model fallback、LiteLLM 的 Router、OpenRouter)全是这个套路——本期用 200 行可读代码把同款机制讲透。

## 变更内容

累积式:`step13 = step12 + 多模型路由`,记忆层/技能层/TodoList/子代理/Agent Team/MCP/Hooks 代码未改。

| 变更点 | 第 12 期 | 第 13 期 |
|---|---|---|
| 工具数量 | 22 | +3 → 25(list_models / switch_model / cost_report) |
| 新增数据结构 | HOOKS / SENSITIVE_WORDS | `MODEL_REGISTRY` / `TIER_DEFAULT` / `BACKGROUND_MODEL` / `ROUTER_STATE` / `HEAVY_KEYWORDS` / `USAGE_LOG_PATH` |
| LLM 调用方式 | 4 处直调 `client.chat.completions.create(model=MODEL)` | 全部收编 `llm_call`(路由→调用→记账),返回 (response, model_id) |
| system prompt | + Hooks 指南 | + 多模型路由指南(何时 list_models/switch_model/cost_report) |
| 启动信息 | 单模型 + Hooks 面板 | 模型池 + 路由模式 + 模型路由面板 + 成本面板 |
| 新增函数 | 10 个 Hooks 函数 | `route_model` / `resolve_model` / `_record_route` / `llm_call` / `log_usage` / `_load_usage_entries` / `list_models` / `switch_model` / `cost_report` / `print_model_panel` / `print_cost_panel` |
| 新增命令 | hooks / audit | + models(模型路由面板)/ cost(成本面板) |
| 演示场景 | 敏感词拦截 + 审计回放 | 换成本期专属:① 简单任务→light 档 ② 复杂任务→heavy 档 + cost_report |
| 落盘文件 | audit_log.jsonl | + memory/usage_log.jsonl(每次 LLM 调用成本) |
| 退出行为 | 打印 audit_log 路径 | + 打印 usage_log 路径提示 |

文件:`code/step13_model_router.py`

## 试一试

1. **看路由分档**:启动程序,自动演示 1(问时间+算数)看 `[路由] 简单任务... → deepseek-chat`;自动演示 2(含"分析")看 `[路由] 命中复杂关键词 → deepseek-reasoner`——同一次启动,两个档位,注意模型路由面板的 ◀ 当前 标记换了位置。
2. **看成本对比**:演示 2 结束后看成本面板最后三行——"实际成本 / 若全用最贵 / 路由省下"。然后输入 `cost` 命令随时再看,或问 Alex"这次对话花了多少钱"让它调 cost_report。
3. **手动钉死模型**:问 Alex"接下来都用 claude-sonnet 回答"——它会调 switch_model(mode='manual', model_id='claude-sonnet')。之后随便问个问题,看 `[路由] manual 模式:钉住 claude-sonnet`。再让它"恢复自动",看路由回到 auto。
4. **看后台任务省钱**:输入 `dispatch` 后问一个需要子代理的问题(如"帮我审查这段代码并整理文档"),看子代理调用时的 `[路由] 后台任务(subagent)固定走 light 档省钱`——受限小任务不烧旗舰的钱。
5. **改路由规则**:`HEAVY_KEYWORDS` 里加一个"翻译",重启后问"帮我翻译这句话"——观察从 light 升到 heavy 的变化;把 `HEAVY_LENGTH_THRESHOLD` 从 400 改成 20,看几乎全部路由到 heavy,成本面板会发生什么。
6. **改价格重新记账**:把 claude-sonnet 的 `input_price` 改成 30,重启看成本面板的"假想账单"翻倍——注册表是唯一的定价真源。
7. **读原始账单**:`cat memory/usage_log.jsonl`,每行一条 JSON——时间/模型/任务/输入输出 token/美元成本,这就是 FinOps 部门要的对账明细。
8. **思考题**:为什么 cost_report 读到的数字和它自己回答后的面板数字差一点(答案:读报表这次调用也被记账)?如果要在 manual 模式下对后台任务"开小灶"(比如子代理也用 heavy),该改 resolve_model 的哪一行?生产环境的两级路由(light 模型先分类,再决定 light/heavy)相比关键词规则,优势和代价各是什么?
