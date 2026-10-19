#!/usr/bin/env python3
"""step13_model_router.py — 第 13 期:多模型路由(自动选模型 + 成本 telemetry)

原理一句话:把"用哪个模型回答"从写死的一个常量,升级成一张"模型注册表 +
路由器"—— 简单任务自动路由到便宜模型,硬骨头自动路由到旗舰模型;每次
LLM 调用都按 token × 单价记账落盘,成本面板随时对比各模型花了多少钱。

本期目标:
1. 解决第 01 期以来一直写死的问题:client 初始化只有一个 MODEL 常量,
   全部对话(含 compact / 子代理 / 队友)都走同一个模型 —— 查个时间也用
   旗舰模型,月底账单吓一跳,且想换供应商要改一堆代码
2. 引入多模型路由层 + 三个新增工具:
   - list_models  — 查看模型注册表(档位/单价/强项)
   - switch_model — 手动切换模型(可切回 auto 自动路由)
   - cost_report  — 查看成本报表(按模型聚合 token 与费用,含"省钱"对比)
3. 三个核心概念:
   - 模型注册表(MODEL_REGISTRY):model_id → 档位(light/heavy)、
     输入/输出单价(美元/百万 token)、强项、api_model(真实端点映射)
   - 路由器(route_model):按任务特征自动选模型 ——
     命中复杂关键词(审查/分析/设计/对比...)或长消息 → heavy 档;
     其余 → light 档(便宜)。compact / 子代理 / 队友等"后台任务"
     统一走 light 档省钱 → 路由策略本身就是省钱策略
   - 成本 telemetry(log_usage):每次 LLM 调用记 token 数 × 注册表单价
     → 成本,追加 usage_log.jsonl;面板对比"实际花费 vs 全用最贵模型"
4. 生产化场景(本期核心卖点):
   - 成本控制:90% 的日常对话用便宜模型,10% 的硬任务才上旗舰 →
     账单直接砍一个量级
   - 供应商自由:注册表里 claude-sonnet / local-qwen 教学环境用 DeepSeek
     端点模拟(api_model 映射),生产环境换 base_url + api_key 即可,
     Agent 主逻辑一行不改
   - 成本可观测:usage_log.jsonl = 每一次"思考"的账单,出了事能对账
5. 关键差异(第 12 期单模型 vs 本期多模型):
   - 第 12 期:MODEL 常量写死,所有调用同一模型、同一价格,无感知
   - 本期:每次调用先过路由器选模型,调用后过记账器落盘,成本面板
     随时回答"今天烧了多少钱、都花在哪个模型上"
6. 演示场景(本期专属,与前几期"演示只做本期"的惯例一致):
   - 演示 1:简单任务自动路由(问时间 → light 档 deepseek-chat,
     便宜模型被选中,账单落盘)
   - 演示 2:复杂任务自动路由(技术决策分析 → heavy 档 deepseek-reasoner,
     旗舰模型被选中)+ cost_report 汇报"实际花费 vs 全用旗舰的对比"

累积式:step13 = step12 + 多模型路由
(保留 step12 的三层记忆、技能层、工具循环、TodoList、子代理、Agent Team、
 MCP、Hooks 全部逻辑,只新增路由层 + 3 个工具 + 成本日志)

运行:
    python code/step13_model_router.py
"""
import os
import sys
import json
import ast
import operator
import subprocess
import time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

load_dotenv()

# ============ 1. 初始化客户端(第 01-12 期)+ 多模型注册表(本期新增)============
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)

# ---------- 第 13 期核心:模型注册表 ----------
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

# 兼容保留:旧代码展示用的默认模型名(现在真正的选择权在路由器手里)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

MAX_ROUNDS = 10  # 会话内短期记忆上限(轮数)

# ============ 2. 路径(与第 07-09 期一致)============
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
MEMORY_DIR = Path(__file__).parent.parent / "memory"
SOUL_PATH = TEMPLATES_DIR / "SOUL.md"
USER_PATH = TEMPLATES_DIR / "USER.md"
COMPACT_PROMPT_PATH = TEMPLATES_DIR / "compact_prompt.md"
TODO_GUIDE_PATH = TEMPLATES_DIR / "todo_guide.md"
RAW_HISTORY_PATH = MEMORY_DIR / "raw_history.jsonl"
SESSIONS_PATH = MEMORY_DIR / "sessions.json"
AUDIT_LOG_PATH = MEMORY_DIR / "audit_log.jsonl"  # 第 12 期:操作审计日志(After Hook 落盘)
USAGE_LOG_PATH = MEMORY_DIR / "usage_log.jsonl"  # 第 13 期:成本 telemetry(每次 LLM 调用记账)

# 压缩触发阈值(轮数);超过后自动调用 compact
COMPACT_THRESHOLD_ROUNDS = 8

# 配置约束:触发阈值必须 < 保留上限
assert COMPACT_THRESHOLD_ROUNDS < MAX_ROUNDS, (
    f"COMPACT_THRESHOLD_ROUNDS({COMPACT_THRESHOLD_ROUNDS}) 必须 < "
    f"MAX_ROUNDS({MAX_ROUNDS}),否则 compact 永远不切窗口"
)


# ============ 3. System Prompt(人设 + 技能索引 + 用户画像)============
def load_system_prompt() -> str:
    """读取 SOUL.md(第 04 期)作为人设核心。"""
    if not SOUL_PATH.exists():
        raise FileNotFoundError(f"找不到人设文件: {SOUL_PATH}")
    return SOUL_PATH.read_text(encoding="utf-8")


def load_user_profile() -> str:
    """读取 templates/USER.md 作为长期记忆 / 用户画像(第 07 期)。"""
    if not USER_PATH.exists():
        return ""
    return USER_PATH.read_text(encoding="utf-8")


# ============ 4. 技能层(与第 06 期完全一致)============
SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}
    meta = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def load_skills_index() -> list[dict]:
    if not SKILLS_DIR.exists():
        return []
    index = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        meta = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        if not meta.get("name"):
            continue
        index.append({
            "name": meta["name"],
            "description": meta.get("description", ""),
            "path": str(skill_dir),
            "size_chars": skill_md.stat().st_size,
        })
    return index


def build_skills_prompt(index: list[dict]) -> str:
    if not index:
        return ""
    lines = [
        "",
        "## 可用技能(Skills)",
        "以下技能默认未加载。如果当前任务与某个技能相关,",
        "先调用 load_skill 工具加载全文,再按技能内容回答:",
    ]
    for s in index:
        lines.append(f"- {s['name']}: {s['description']}")
    return "\n".join(lines)


# ============ 5. 记忆层(与第 07 期完全一致)============
def load_compact_prompt() -> str:
    """读取 compact 提示词模板。"""
    if not COMPACT_PROMPT_PATH.exists():
        return "请把上面的对话压缩成简洁的第三人称摘要,保留关键事实、偏好和未完成任务。"
    return COMPACT_PROMPT_PATH.read_text(encoding="utf-8")


def append_raw_history(session_id: str, role: str, content: str, extra: dict | None = None):
    """原始历史落盘(append-only JSONL)。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "session": session_id,
        "role": role,
        "content": content,
    }
    if extra:
        record.update(extra)
    with RAW_HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _extract_content(m: dict) -> str:
    """把一条 message 折成可落盘的 content 字符串。"""
    content = m.get("content") or ""
    if m.get("tool_calls"):
        calls = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
        content = f"[tool_calls: {calls}]"
    if not content:
        content = f"({m.get('role', '?')}: 空内容)"
    return content


def persist_one(m: dict, session_id: str, source: str, max_chars: int = 500):
    """单条消息立即落盘。事件溯源:append 即写,不攒不延迟。"""
    if m.get("role") == "system":
        return False
    content = _extract_content(m)
    if not content:
        return False
    try:
        append_raw_history(session_id, m["role"], content[:max_chars],
                           extra={"source": source})
        return True
    except Exception:
        return False


def append_and_persist(messages: list[dict], msg: dict,
                       session_id: str | None, source: str = "produced"):
    """唯一的"对话事件"入口:append 到 messages + 立即落盘。"""
    messages.append(msg)
    if session_id:
        return persist_one(msg, session_id, source=source)
    return False


def log_session(session_id: str, rounds: int, prompt_tokens: int, completion_tokens: int):
    """会话元数据 upsert 到 sessions.json。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    sessions = []
    if SESSIONS_PATH.exists():
        try:
            sessions = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            sessions = []

    record = {
        "session_id": session_id,
        "ended_at": datetime.now().isoformat(timespec="seconds"),
        "rounds": rounds,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    for i, s in enumerate(sessions):
        if s.get("session_id") == session_id:
            sessions[i] = record
            SESSIONS_PATH.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
            return
    sessions.append(record)
    SESSIONS_PATH.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")


def count_raw_history_lines() -> int:
    """统计原始历史行数(给可视化面板用)。"""
    if not RAW_HISTORY_PATH.exists():
        return 0
    with RAW_HISTORY_PATH.open(encoding="utf-8") as f:
        return sum(1 for _ in f)


def read_recent_raw_history(limit: int = 20) -> list[dict]:
    """读取最近 N 条原始历史(用于 memory 命令展示)。"""
    if not RAW_HISTORY_PATH.exists():
        return []
    lines = RAW_HISTORY_PATH.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(l) for l in lines[-limit:] if l.strip()]


def compact_history(messages: list[dict]) -> list[dict]:
    """压缩早期对话:用 LLM 把历史 messages 摘要化,替换掉早期的几轮。"""
    if not messages or messages[0]["role"] != "system":
        return messages
    system_msg = messages[0]
    convo = messages[1:]
    user_rounds = sum(1 for m in convo if m["role"] == "user")
    if user_rounds <= COMPACT_THRESHOLD_ROUNDS:
        return messages

    user_positions = [i for i, m in enumerate(convo) if m["role"] == "user"]
    if len(user_positions) < MAX_ROUNDS:
        return messages
    boundary = user_positions[-MAX_ROUNDS]
    to_compress = convo[:boundary]
    keep = convo[boundary:]

    if not to_compress:
        return messages

    compact_input_lines = []
    for m in to_compress:
        role = m["role"]
        content = m.get("content") or ""
        if m.get("tool_calls"):
            calls = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
            content = f"[tool_calls: {calls}]"
        compact_input_lines.append(f"[{role}] {content}")
    compact_input = "\n".join(compact_input_lines)

    compact_prompt = load_compact_prompt()
    try:
        # 第 13 期:压缩也走 llm_call → task="compact" 固定路由到 light 档(省钱)
        resp, _ = llm_call(
            [
                {"role": "system", "content": compact_prompt},
                {"role": "user", "content": f"请压缩以下对话历史:\n\n{compact_input}"},
            ],
            task="compact",
            max_tokens=600,
        )
        summary = resp.choices[0].message.content
    except Exception as e:
        summary = f"[compact 失败: {e}]"

    new_messages = [system_msg]
    new_messages.append({
        "role": "assistant",
        "content": f"[compact summary] 下面是早期对话的摘要,用于节省上下文:\n\n{summary}",
    })
    new_messages.extend(keep)
    return new_messages


# ============ 6. TodoList 层(与第 08 期完全一致)============
# 任务状态常量
TODO_PENDING = "pending"
TODO_IN_PROGRESS = "in_progress"
TODO_COMPLETED = "completed"

# 运行时内存(纯内存,不持久化,用完即弃)
_todo_list: list[dict] = []
# 每个 task: {"id": int, "content": str, "status": str, "created_at": str}


def _next_todo_id(tasks: list[dict]) -> int:
    """生成下一个任务 ID(从 1 开始,单调递增)。"""
    if not tasks:
        return 1
    return max(t["id"] for t in tasks) + 1


def _todo_snapshot(tasks: list[dict]) -> list[dict]:
    """返回任务列表的快照(精简版)。"""
    return [{"id": t["id"], "content": t["content"], "status": t["status"]} for t in tasks]


def todo_create(tasks: list[str]) -> str:
    """批量创建待办任务。

    约束:创建时全部为 pending,后续通过 todo_update 逐个推进。
    """
    global _todo_list
    created = []
    for content in tasks:
        task = {
            "id": _next_todo_id(_todo_list),
            "content": content,
            "status": TODO_PENDING,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        _todo_list.append(task)
        created.append(task)
    return json.dumps({
        "created": len(created),
        "tasks": [{"id": t["id"], "content": t["content"], "status": t["status"]} for t in created],
    }, ensure_ascii=False)


def todo_update(task_id: int, new_status: str) -> str:
    """更新任务状态,强制两条约束:

    约束 1(顺序):设为 in_progress 时,排在前面的任务必须全部 completed。
                   不允许跳过 #1 直接开 #3,强制一条条往下执行。
    约束 2(单 in_progress):同一时刻只能有一个任务处于 in_progress。
                   设新的 in_progress 时,旧的自动退回 pending。
    """
    global _todo_list

    # 校验状态值
    valid_statuses = {TODO_PENDING, TODO_IN_PROGRESS, TODO_COMPLETED}
    if new_status not in valid_statuses:
        return json.dumps({"error": f"无效状态: {new_status}", "valid": list(valid_statuses)}, ensure_ascii=False)

    # 找到目标任务
    target = None
    for t in _todo_list:
        if t["id"] == task_id:
            target = t
            break
    if target is None:
        return json.dumps({"error": f"任务不存在: id={task_id}"}, ensure_ascii=False)

    # 约束 1:顺序约束 —— 前面的任务必须全部完成才能开始
    if new_status == TODO_IN_PROGRESS:
        blocked_by = []
        for t in _todo_list:
            if t["id"] < task_id and t["status"] != TODO_COMPLETED:
                blocked_by.append(t["id"])
        if blocked_by:
            return json.dumps({
                "error": f"顺序约束:任务 #{task_id} 前面还有未完成任务 {blocked_by}",
                "hint": "必须按顺序逐个完成,不能跳过",
                "blocked_by": blocked_by,
                "current_list": _todo_snapshot(_todo_list),
            }, ensure_ascii=False)

    # 约束 2:单 in_progress 约束 —— 把其他 in_progress 的退回 pending
    if new_status == TODO_IN_PROGRESS:
        for t in _todo_list:
            if t["status"] == TODO_IN_PROGRESS and t["id"] != task_id:
                t["status"] = TODO_PENDING
                print(f"  [todo] 任务 #{t['id']} 自动从 in_progress 退回 pending(单 in_progress 约束)")

    target["status"] = new_status

    # 全部完成后自动清空,回到初始状态(用完即弃)
    if new_status == TODO_COMPLETED and all(t["status"] == TODO_COMPLETED for t in _todo_list):
        completed_snapshot = _todo_snapshot(_todo_list)
        _todo_list.clear()
        print("  [todo] 所有任务已完成,列表已清空")
        return json.dumps({
            "updated": task_id,
            "new_status": new_status,
            "all_completed": True,
            "cleared": True,
            "completed_tasks": completed_snapshot,
        }, ensure_ascii=False)

    return json.dumps({
        "updated": task_id,
        "content": target["content"],
        "new_status": new_status,
        "current_list": _todo_snapshot(_todo_list),
    }, ensure_ascii=False)


def todo_list() -> str:
    """查看当前任务列表及进度。"""
    return json.dumps({
        "total": len(_todo_list),
        "pending": sum(1 for t in _todo_list if t["status"] == TODO_PENDING),
        "in_progress": sum(1 for t in _todo_list if t["status"] == TODO_IN_PROGRESS),
        "completed": sum(1 for t in _todo_list if t["status"] == TODO_COMPLETED),
        "tasks": _todo_snapshot(_todo_list),
    }, ensure_ascii=False)


def print_todo_panel():
    """打印 TodoList 面板(教学可视化)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  📋 Alex 任务面板(第 08 期)                            │")
    print("├" + "─" * 58 + "┤")

    if not _todo_list:
        print("│  (空)还没有任务,让 Alex 帮你规划                     │")
    else:
        for t in _todo_list:
            icon = {"pending": "⬚", "in_progress": "▶", "completed": "✓"}[t["status"]]
            line = f"  {icon} #{t['id']} {t['content']}"
            if len(line) > 54:
                line = line[:51] + "..."
            print(f"│{line:<56}│")

        # 进度统计
        total = len(_todo_list)
        done = sum(1 for t in _todo_list if t["status"] == TODO_COMPLETED)
        if total > 0:
            bar_len = 40
            filled = int(bar_len * done / total)
            bar = "█" * filled + "░" * (bar_len - filled)
            pct = done * 100 // total
            print("│" + "─" * 58 + "│")
            print(f"│  {bar} {pct:3d}% ({done}/{total})                          │")
    print("└" + "─" * 58 + "┘")
    print()


# ============ 7. Subagent 层(与第 09 期完全一致)============
# 子代理类型注册表:预定义两种专家
SUBAGENT_TYPES = {
    "code_reviewer": {
        "description": "代码审查员。审查代码质量,指出 bug、性能、安全问题,给出改进建议。",
        "allowed_tools": ["get_current_time", "calculate", "load_skill"],
        "extra_prompt": (
            "\n\n## 子代理角色:代码审查员\n"
            "你是代码审查专家。你的职责:\n"
            "1. 审查代码逻辑正确性\n"
            "2. 指出潜在 bug 和边界情况\n"
            "3. 检查性能问题\n"
            "4. 给出具体的改进建议\n\n"
            "你不能创建任务(todo_create)、不能修改用户画像、不能派遣其他子代理。\n"
        ),
    },
    "doc_writer": {
        "description": "文档整理员。整理文档结构、优化措辞、生成摘要、规范格式。",
        "allowed_tools": ["get_current_time", "calculate", "load_skill"],
        "extra_prompt": (
            "\n\n## 子代理角色:文档整理员\n"
            "你是技术文档专家。你的职责:\n"
            "1. 整理文档结构(标题层级、段落划分)\n"
            "2. 优化措辞(技术术语准确、表达简洁)\n"
            "3. 生成摘要(提炼核心要点)\n"
            "4. 规范格式(markdown 格式正确)\n\n"
            "你不能创建任务、不能修改用户画像、不能派遣其他子代理。\n"
        ),
    },
}

# 子代理运行时记录(可视化用)
_subagent_runs: list[dict] = []


def _run_subagent(agent_type: str, task: str) -> tuple[str, int, int]:
    """执行单个子代理(独立上下文 + 工具白名单)。

    三个核心概念:
    1. 独立上下文:全新 messages,不复用主 Agent 对话历史
    2. 工具白名单:只保留 allowed_tools 中的工具定义(LLM 看不到未授权工具)
    3. 自己的工具循环:复用第 05 期的 while True 循环,但用受限工具集

    返回:(结果文本, prompt_tokens, completion_tokens)
    """
    config = SUBAGENT_TYPES[agent_type]

    # ① 独立上下文:全新 messages,不复用主 Agent 对话历史
    sub_system = load_system_prompt() + config["extra_prompt"]
    skills_index = load_skills_index()
    skills_prompt = build_skills_prompt(skills_index)
    if skills_prompt:
        sub_system += "\n" + skills_prompt

    sub_messages = [
        {"role": "system", "content": sub_system},
        {"role": "user", "content": task},
    ]

    # ② 工具白名单:只保留 allowed_tools 中的工具
    allowed = set(config["allowed_tools"])
    sub_tools = [t for t in TOOLS if t["function"]["name"] in allowed]

    # ③ 子代理工具循环(复用主循环逻辑,但用受限工具集和独立 messages)
    # 第 13 期:LLM 调用走 llm_call → task="subagent" 固定路由到 light 档(省钱)
    total_prompt = 0
    total_completion = 0

    while True:
        response, _ = llm_call(sub_messages, task="subagent",
                               tools=sub_tools, max_tokens=2048)
        msg = response.choices[0].message
        total_prompt += response.usage.prompt_tokens
        total_completion += response.usage.completion_tokens

        if msg.tool_calls:
            sub_messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })
            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)
                print(f"    [子代理·{agent_type}] 工具调用: {fn_name}({fn_args})")
                result = execute_tool(fn_name, fn_args)
                display = result if len(result) <= 80 else result[:80] + "..."
                print(f"    [子代理·{agent_type}] 结果: {display}")
                sub_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
            continue

        sub_messages.append({"role": "assistant", "content": msg.content})
        return msg.content, total_prompt, total_completion


def dispatch_subagent(agent_type: str, task: str) -> str:
    """派遣单个子代理(同步,阻塞直到完成)。

    适用场景:只需要一个专家完成特定任务时使用。
    """
    if agent_type not in SUBAGENT_TYPES:
        return json.dumps({
            "error": f"未知子代理类型: {agent_type}",
            "available": list(SUBAGENT_TYPES.keys()),
        }, ensure_ascii=False)

    print(f"  [子代理] 派遣 {agent_type}...")
    start = time.time()
    result, p, c = _run_subagent(agent_type, task)
    elapsed = time.time() - start

    _subagent_runs.append({
        "type": agent_type,
        "task": task[:200],
        "result": result,
        "prompt_tokens": p,
        "completion_tokens": c,
        "elapsed": round(elapsed, 2),
        "parallel": False,
    })

    return json.dumps({
        "agent_type": agent_type,
        "result": result,
        "prompt_tokens": p,
        "completion_tokens": c,
        "elapsed_seconds": round(elapsed, 2),
    }, ensure_ascii=False)


def dispatch_subagents_parallel(tasks: list[dict]) -> str:
    """并发派遣多个子代理。

    tasks: [{"agent_type": "code_reviewer", "task": "审查这段代码..."}, ...]

    使用 ThreadPoolExecutor 实现真正的并发执行:
    - 每个子代理在自己的线程中运行
    - 总耗时 ≈ max(各子代理耗时),不是 sum(各子代理耗时)
    - 对比串行:串行总耗时 = sum,并发总耗时 = max
    """
    if not tasks or not isinstance(tasks, list):
        return json.dumps({"error": "tasks 必须是非空列表"}, ensure_ascii=False)

    # 验证所有类型
    for t in tasks:
        agent_type = t.get("agent_type", "")
        if agent_type not in SUBAGENT_TYPES:
            return json.dumps({
                "error": f"未知子代理类型: {agent_type}",
                "available": list(SUBAGENT_TYPES.keys()),
            }, ensure_ascii=False)

    n = len(tasks)
    print(f"  [子代理] 并发派遣 {n} 个子代理: {', '.join(t['agent_type'] for t in tasks)}")
    start_time = time.time()

    results = [None] * n  # 预分配,保持原始顺序

    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = {}
        for i, t in enumerate(tasks):
            future = executor.submit(_run_subagent, t["agent_type"], t["task"])
            futures[future] = i

        for future in as_completed(futures):
            idx = futures[future]
            try:
                result, p, c = future.result()
                _subagent_runs.append({
                    "type": tasks[idx]["agent_type"],
                    "task": tasks[idx]["task"][:200],
                    "result": result,
                    "prompt_tokens": p,
                    "completion_tokens": c,
                    "elapsed": None,  # 填在汇总里
                    "parallel": True,
                })
                results[idx] = {
                    "index": idx,
                    "agent_type": tasks[idx]["agent_type"],
                    "result": result,
                    "prompt_tokens": p,
                    "completion_tokens": c,
                }
            except Exception as e:
                results[idx] = {
                    "index": idx,
                    "agent_type": tasks[idx]["agent_type"],
                    "error": str(e),
                }

    elapsed = time.time() - start_time

    # 填充每个 run 的 elapsed(并发场景下统一用总耗时)
    for run in _subagent_runs[-n:]:
        if run.get("parallel"):
            run["elapsed"] = round(elapsed, 2)

    return json.dumps({
        "dispatched": n,
        "elapsed_seconds": round(elapsed, 2),
        "results": results,
    }, ensure_ascii=False)


def list_subagents() -> str:
    """列出可用的子代理类型及其工具白名单。"""
    return json.dumps({
        "available": [
            {
                "type": name,
                "description": config["description"],
                "allowed_tools": config["allowed_tools"],
            }
            for name, config in SUBAGENT_TYPES.items()
        ]
    }, ensure_ascii=False)


def print_subagent_panel():
    """打印子代理面板(教学可视化)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  🔀 Alex 子代理面板(第 09 期)                        │")
    print("├" + "─" * 58 + "┤")

    # 可用类型
    types = list(SUBAGENT_TYPES.keys())
    print(f"│  可用类型: {', '.join(types):<40s}│")
    print("│" + "─" * 58 + "│")

    if not _subagent_runs:
        print("│  (空)还没有派遣过子代理                               │")
    else:
        for run in _subagent_runs[-5:]:  # 最近 5 个
            short_task = run["task"][:30] + "..." if len(run["task"]) > 30 else run["task"]
            mode = "并发" if run.get("parallel") else "单独"
            line = f"  {run['type']:14s} [{mode}] {short_task}"
            print(f"│{line:<56}│")
            tokens = run["prompt_tokens"] + run["completion_tokens"]
            elapsed = run.get("elapsed", 0)
            line2 = f"    token={tokens}  耗时={elapsed}s  结果={len(run['result'])}字符"
            print(f"│{line2:<56}│")

    print("└" + "─" * 58 + "┘")
    print()


# ============ 7b. Agent Team 层(第 10 期引入,本期沿用)============
# 团队成员注册表:持久队友(有 inbox + 持久 messages + 唤醒计数)
#
# 与 Subagent 的核心区别:
# ┌──────────────┬─────────────────────┬──────────────────────────────┐
# │              │ Subagent(第09期)    │ Team Member(本期)            │
# ├──────────────┼─────────────────────┼──────────────────────────────┤
# │ 生命周期     │ 临时:派遣→运行→销毁  │ 持久:注册一次,可多次唤醒     │
# │ 上下文       │ 每次 dispatch 全新    │ messages 跨唤醒保持(累积)    │
# │ 通信         │ 单向(主→子→结果)    │ 双向(inbox 异步消息)        │
# │ 状态         │ 无状态                │ 有 inbox + 持久 messages     │
# │ 协作         │ 主 Agent 汇总        │ 队友通过 inbox 直接协作       │
# └──────────────┴─────────────────────┴──────────────────────────────┘
TEAM_MEMBERS: dict[str, dict] = {
    "frontend_dev": {
        "description": "前端开发工程师。负责页面结构、样式、交互逻辑,能与其他队友通过消息协作。",
        "allowed_tools": ["get_current_time", "calculate", "load_skill",
                          "send_message", "broadcast_message"],
        "extra_prompt": (
            "\n\n## 团队角色:前端开发工程师\n"
            "你是团队的前端开发成员。你的职责:\n"
            "1. 设计页面结构(HTML 骨架)\n"
            "2. 编写样式和交互逻辑\n"
            "3. 与后端队友协作:需要接口时通过 send_message 询问\n"
            "4. 收到后端回复后对接 API\n\n"
            "你可以给其他队友发消息(send_message)或广播(broadcast_message)。\n"
            "你的对话历史会跨唤醒保持,下次被唤醒时还记得之前做了什么。\n"
        ),
        "inbox": [],       # 持久 inbox:其他队友发来的消息
        "messages": None,  # 持久对话历史:None = 尚未被唤醒过
        "wake_count": 0,   # 被唤醒次数
    },
    "backend_dev": {
        "description": "后端开发工程师。负责 API 接口、数据模型、业务逻辑,能与其他队友通过消息协作。",
        "allowed_tools": ["get_current_time", "calculate", "load_skill",
                          "send_message", "broadcast_message"],
        "extra_prompt": (
            "\n\n## 团队角色:后端开发工程师\n"
            "你是团队的后端开发成员。你的职责:\n"
            "1. 设计 API 接口(路由、请求/响应格式)\n"
            "2. 编写数据模型和业务逻辑\n"
            "3. 与前端队友协作:收到询问后通过 send_message 回复接口定义\n"
            "4. 确保接口安全(输入校验、错误处理)\n\n"
            "你可以给其他队友发消息(send_message)或广播(broadcast_message)。\n"
            "你的对话历史会跨唤醒保持,下次被唤醒时还记得之前做了什么。\n"
        ),
        "inbox": [],
        "messages": None,
        "wake_count": 0,
    },
}

# 当前正在运行的队友 ID(用于 send_message 判断发送者)
# 主 Agent 调用 send_message 时,_current_member_id 为 None → 发送者为 "team_lead"
_current_member_id: str | None = None


def _run_team_member(member_id: str, task: str) -> tuple[str, int, int]:
    """执行一个持久队友(持久 messages + inbox 注入 + 工具白名单)。

    与 _run_subagent 的三个关键区别:
    1. 持久上下文:messages 跨唤醒保持(不像 subagent 每次 dispatch 全新)
    2. inbox 注入:唤醒时把未读消息注入到 task 前面,读后清空
    3. 通信工具:队友的 allowed_tools 包含 send_message / broadcast_message

    返回:(结果文本, prompt_tokens, completion_tokens)
    """
    global _current_member_id
    config = TEAM_MEMBERS[member_id]

    # ① 初始化持久 messages(仅第一次唤醒时)
    if config["messages"] is None:
        sub_system = load_system_prompt() + config["extra_prompt"]
        skills_index = load_skills_index()
        skills_prompt = build_skills_prompt(skills_index)
        if skills_prompt:
            sub_system += "\n" + skills_prompt
        config["messages"] = [
            {"role": "system", "content": sub_system},
        ]

    # ② inbox 注入:把未读消息拼到 task 前面,读后清空
    if config["inbox"]:
        inbox_lines = []
        for msg in config["inbox"]:
            inbox_lines.append(f"  [{msg['from']}] {msg['content']}")
        inbox_text = (
            "\n\n--- 收到的队友消息 ---\n"
            + "\n".join(inbox_lines)
            + "\n--- 消息结束 ---"
        )
        config["inbox"].clear()  # 读后清空(已消费)
        print(f"  [team·{member_id}] 读取 {len(inbox_lines)} 条 inbox 消息")
    else:
        inbox_text = ""

    # ③ 追加 task 到持久 messages(上下文累积)
    user_content = task + inbox_text
    config["messages"].append({"role": "user", "content": user_content})
    config["wake_count"] += 1

    # ④ 工具白名单(包含通信工具)
    allowed = set(config["allowed_tools"])
    sub_tools = [t for t in TOOLS if t["function"]["name"] in allowed]

    # ⑤ 设置当前队友 ID(供 send_message 判断发送者)
    _current_member_id = member_id

    # ⑥ 队友工具循环(复用第 05 期循环,但用持久 messages + 受限工具)
    # 第 13 期:LLM 调用走 llm_call → task="team" 固定路由到 light 档(省钱)
    total_prompt = 0
    total_completion = 0

    while True:
        response, _ = llm_call(config["messages"], task="team",
                               tools=sub_tools, max_tokens=2048)
        msg = response.choices[0].message
        total_prompt += response.usage.prompt_tokens
        total_completion += response.usage.completion_tokens

        if msg.tool_calls:
            config["messages"].append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })
            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)
                print(f"    [team·{member_id}] 工具调用: {fn_name}({fn_args})")
                result = execute_tool(fn_name, fn_args)
                display = result if len(result) <= 80 else result[:80] + "..."
                print(f"    [team·{member_id}] 结果: {display}")
                config["messages"].append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
            continue

        config["messages"].append({"role": "assistant", "content": msg.content})

        # 清除当前队友 ID
        _current_member_id = None

        return msg.content, total_prompt, total_completion


def team_wake(member_id: str, task: str) -> str:
    """唤醒一个持久队友执行任务。

    与 dispatch_subagent 的关键区别:
    - subagent:每次 dispatch 全新 messages,用完即弃
    - team member:messages 跨唤醒保持,inbox 消息自动注入

    唤醒时:持久 messages 追加新 task + 未读 inbox 消息,队友用自己的工具循环处理。
    """
    if member_id not in TEAM_MEMBERS:
        return json.dumps({
            "error": f"未知团队成员: {member_id}",
            "available": list(TEAM_MEMBERS.keys()),
        }, ensure_ascii=False)

    config = TEAM_MEMBERS[member_id]
    first_wake = config["messages"] is None
    print(f"  [team] 唤醒 {member_id}({'首次' if first_wake else f'第{config['wake_count']+1}次'})...")

    start = time.time()
    result, p, c = _run_team_member(member_id, task)
    elapsed = time.time() - start

    return json.dumps({
        "member_id": member_id,
        "wake_count": config["wake_count"],
        "result": result,
        "prompt_tokens": p,
        "completion_tokens": c,
        "elapsed_seconds": round(elapsed, 2),
    }, ensure_ascii=False)


def send_message(to_member: str, content: str) -> str:
    """给指定队友的 inbox 发送一条消息。

    主 Agent 和队友都能调用:
    - 主 Agent 调用时:发送者为 "team_lead"
    - 队友调用时:发送者为自己的 member_id(由 _current_member_id 确定)

    消息进入对方的 inbox,对方下次被唤醒时自动看到。
    """
    if to_member not in TEAM_MEMBERS:
        return json.dumps({
            "error": f"未知团队成员: {to_member}",
            "available": list(TEAM_MEMBERS.keys()),
        }, ensure_ascii=False)

    sender = _current_member_id or "team_lead"
    msg_record = {
        "from": sender,
        "content": content,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    TEAM_MEMBERS[to_member]["inbox"].append(msg_record)

    print(f"  [team] {sender} → {to_member} inbox: {content[:60]}")

    return json.dumps({
        "sent": True,
        "from": sender,
        "to": to_member,
        "content": content[:200],
        "inbox_size": len(TEAM_MEMBERS[to_member]["inbox"]),
    }, ensure_ascii=False)


def broadcast_message(content: str) -> str:
    """向所有队友的 inbox 广播一条消息。

    一条消息同时进入所有队友的 inbox。
    适用场景:需求变更、全局通知、进度同步等。
    """
    sender = _current_member_id or "team_lead"
    delivered = []
    for member_id, config in TEAM_MEMBERS.items():
        if member_id == sender:
            continue  # 不给自己发
        config["inbox"].append({
            "from": sender,
            "content": content,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "broadcast": True,
        })
        delivered.append(member_id)

    print(f"  [team] {sender} 广播给 {len(delivered)} 个队友: {content[:60]}")

    return json.dumps({
        "broadcast": True,
        "from": sender,
        "delivered_to": delivered,
        "content": content[:200],
    }, ensure_ascii=False)


def read_inbox(member_id: str) -> str:
    """读取某个队友的 inbox 消息(不消费,仅查看)。"""
    if member_id not in TEAM_MEMBERS:
        return json.dumps({
            "error": f"未知团队成员: {member_id}",
            "available": list(TEAM_MEMBERS.keys()),
        }, ensure_ascii=False)

    inbox = TEAM_MEMBERS[member_id]["inbox"]
    return json.dumps({
        "member_id": member_id,
        "unread_count": len(inbox),
        "messages": inbox,
    }, ensure_ascii=False)


def list_team() -> str:
    """列出团队成员及其状态(inbox 消息数、唤醒次数)。"""
    return json.dumps({
        "members": [
            {
                "member_id": mid,
                "description": config["description"],
                "inbox_unread": len(config["inbox"]),
                "wake_count": config["wake_count"],
                "has_context": config["messages"] is not None,
            }
            for mid, config in TEAM_MEMBERS.items()
        ]
    }, ensure_ascii=False)


def print_team_panel():
    """打印团队面板(教学可视化)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  👥 Alex 团队面板(第 10 期新增)                      │")
    print("├" + "─" * 58 + "┤")

    members = list(TEAM_MEMBERS.keys())
    print(f"│  团队成员: {', '.join(members):<42s}│")
    print("│" + "─" * 58 + "│")

    for mid, config in TEAM_MEMBERS.items():
        inbox_n = len(config["inbox"])
        wakes = config["wake_count"]
        has_ctx = "有" if config["messages"] is not None else "无"
        line = f"  {mid:14s} inbox={inbox_n} 唤醒={wakes} 上下文={has_ctx}"
        print(f"│{line:<56}│")

    print("│" + "─" * 58 + "│")

    # 显示最近的 inbox 消息
    all_msgs = []
    for mid, config in TEAM_MEMBERS.items():
        for msg in config["inbox"]:
            all_msgs.append((mid, msg))
    if all_msgs:
        print(f"│  未读消息({len(all_msgs)} 条):{'':<34}│")
        for mid, msg in all_msgs[-3:]:
            short = f"{msg['from']}→{mid}: {msg['content'][:30]}"
            if len(short) > 50:
                short = short[:47] + "..."
            print(f"│    {short:<52}│")
    else:
        print("│  (空)inbox 里没有未读消息                              │")

    print("└" + "─" * 58 + "┘")
    print()


# ============ 7c. MCP 层(第 11 期引入,本期沿用)============
# MCP(Model Context Protocol):连接"外部工具服务器"的开放协议
#
# 与内置工具(第 05 期 Function Call)的核心区别:
# ┌──────────────┬───────────────────────────┬───────────────────────────────┐
# │              │ 内置工具(第05期)          │ MCP 工具(本期)               │
# ├──────────────┼───────────────────────────┼───────────────────────────────┤
# │ 工具住哪     │ Agent 进程里的函数          │ 外部服务器进程里               │
# │ 加工具       │ 改 Agent 源码 + 注册 schema │ 连一个 MCP 服务器              │
# │ 通信方式     │ 直接函数调用(进程内)       │ stdio JSON-RPC(跨进程)       │
# │ 谁维护       │ 自己写、自己维护            │ 别人写、拿来就用               │
# │ 换供应商     │ 重写工具函数                │ 换一条连接命令                 │
# └──────────────┴───────────────────────────┴───────────────────────────────┘
#
# stdio transport 一图流:
#   ┌──────────── Agent 进程(step11)────────────┐
#   │  mcp_call_tool("repo", "get_repository_info",│
#   │                {"owner": "deepseek-ai", ...})│
#   └──────┬─────────────────────────────────────┘
#          │ stdin  ← 每行一条 JSON-RPC 请求
#          ▼
#   ┌──────────── MCP 服务器进程(repo_server.py)──┐
#   │  {"jsonrpc":"2.0","id":3,"method":"tools/call", ...}
#   │  → 查数据 → 组织结果 → 写回 stdout          │
#   └──────┬─────────────────────────────────────┘
#          │ stdout ← 每行一条 JSON-RPC 响应
#          ▼
#   Agent 收到 {"id":3,"result":{"content":[{"type":"text",...}]}}
#
MCP_PROTOCOL_VERSION = "2024-11-05"

# MCP 服务器注册表:server_id → 配置(连接命令 + 运行时状态)
# 预配置一个教学模拟的"GitHub 风格仓库服务器";
# 生产中把 command 换成真实 MCP server(如 GitHub 官方 server),协议完全一样
MCP_SERVERS: dict[str, dict] = {
    "repo": {
        "description": (
            "GitHub 风格仓库信息服务器(教学模拟)。"
            "提供 search_repositories / get_repository_info / list_issues 三个工具。"
        ),
        "command": [sys.executable, str(Path(__file__).parent.parent / "mcp_servers" / "repo_server.py")],
        "conn": None,   # 连接对象(_McpConnection),None = 未连接
        "tools": [],    # initialize 握手后缓存的工具清单
    },
}


class _McpConnection:
    """MCP stdio 连接:子进程 + JSON-RPC 2.0(每行一条 JSON 消息)。

    把子进程的 stdin/stdout 当"电话线":
      Agent → server.stdin   发 JSON 请求
      Agent ← server.stdout  收 JSON 响应
    注意:server 的日志必须打到 stderr——stdout 被协议独占,混进日志会污染协议流。
    """

    def __init__(self, server_id: str, command: list[str]):
        self.server_id = server_id
        self.req_id = 0  # JSON-RPC 请求计数,用来配对"请求/响应"
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # 教学演示时不显示 server 日志;想看可改为 None
            text=True,
            bufsize=1,
        )
        self.server_info: dict = {}
        self.tools: list[dict] = []
        self._initialize()  # 启动后立刻握手

    # ---- 协议底层:发一行 / 收一行 ----
    def _send(self, obj: dict):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue  # 跳过无法解析的行,继续等真正的 JSON-RPC 响应
        raise ConnectionError(f"MCP 服务器 {self.server_id} 已断开(stdout 关闭)")

    def _request(self, method: str, params: dict | None = None) -> dict:
        self.req_id += 1
        msg = {"jsonrpc": "2.0", "id": self.req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        # 服务器可能穿插发别的消息,只认 id 配对的那条响应
        while True:
            resp = self._recv()
            if resp.get("id") == self.req_id:
                if "error" in resp:
                    raise RuntimeError(f"MCP {method} 出错: {resp['error']}")
                return resp.get("result", {})

    # ---- 协议三步:握手 → 拿工具 → 调工具 ----
    def _initialize(self):
        result = self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "alex-agent-step11", "version": "1.0.0"},
        })
        self.server_info = result
        # 握手完成必须回一个 initialized 通知(MCP 规范要求)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # 顺手把工具清单缓存下来(mcp_list_tools 直接读缓存)
        self.tools = self._request("tools/list").get("tools", [])

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        result = self._request("tools/call", {"name": tool_name, "arguments": arguments})
        # MCP 工具结果统一装在 content 数组里,教学场景只取文本部分
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        if result.get("isError"):
            return json.dumps({"error": "\n".join(texts) or "MCP 工具执行失败"}, ensure_ascii=False)
        return "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


# ---- MCP 工具实现(暴露给 LLM 的 4 个) ----
def mcp_connect(server_id: str) -> str:
    config = MCP_SERVERS.get(server_id)
    if config is None:
        return json.dumps({
            "error": f"MCP 服务器不存在: {server_id}",
            "available": ", ".join(MCP_SERVERS.keys()),
        }, ensure_ascii=False)
    if config["conn"] is not None:
        return json.dumps({
            "server": server_id, "status": "already_connected",
            "note": "连接已存在,无需重复连接",
            "tools": [t["name"] for t in config["tools"]],
        }, ensure_ascii=False)
    conn = _McpConnection(server_id, config["command"])
    config["conn"] = conn
    config["tools"] = conn.tools
    return json.dumps({
        "server": server_id,
        "status": "connected",
        "server_info": conn.server_info.get("serverInfo", {}),
        "tools": [{"name": t["name"], "description": t.get("description", "")}
                  for t in conn.tools],
    }, ensure_ascii=False)


def mcp_list_tools(server_id: str) -> str:
    config = MCP_SERVERS.get(server_id)
    if config is None:
        return json.dumps({"error": f"MCP 服务器不存在: {server_id}"}, ensure_ascii=False)
    if config["conn"] is None:
        return json.dumps({
            "error": f"尚未连接 {server_id},请先调用 mcp_connect",
        }, ensure_ascii=False)
    return json.dumps({
        "server": server_id,
        "tools": [
            {"name": t["name"], "description": t.get("description", ""),
             "inputSchema": t.get("inputSchema", {})}
            for t in config["tools"]
        ],
    }, ensure_ascii=False)


def mcp_call_tool(server_id: str, tool_name: str, arguments: dict) -> str:
    config = MCP_SERVERS.get(server_id)
    if config is None:
        return json.dumps({"error": f"MCP 服务器不存在: {server_id}"}, ensure_ascii=False)
    conn = config["conn"]
    if conn is None:
        # 未连接就先自动连上(MCP 的"即插即用"体验)
        conn = _McpConnection(server_id, config["command"])
        config["conn"] = conn
        config["tools"] = conn.tools
    known = [t["name"] for t in config["tools"]]
    if tool_name not in known:
        return json.dumps({
            "error": f"服务器 {server_id} 上没有工具 {tool_name}",
            "available": known,
        }, ensure_ascii=False)
    return conn.call_tool(tool_name, arguments)


def list_mcp_servers() -> str:
    servers = []
    for sid, cfg in MCP_SERVERS.items():
        servers.append({
            "server_id": sid,
            "description": cfg["description"],
            "connected": cfg["conn"] is not None,
            "tool_count": len(cfg["tools"]),
        })
    return json.dumps({"mcp_servers": servers}, ensure_ascii=False)


def shutdown_mcp():
    """程序退出前,把所有 MCP 子进程收掉(不留孤儿进程)。"""
    for sid, cfg in MCP_SERVERS.items():
        if cfg["conn"] is not None:
            cfg["conn"].stop()
            cfg["conn"] = None


def print_mcp_panel():
    """打印 MCP 面板(教学可视化)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  🔌 Alex MCP 面板(第 11 期新增)                          │")
    print("├" + "─" * 58 + "┤")
    print(f"│  MCP 服务器注册表: {', '.join(MCP_SERVERS.keys())}")
    print("│" + "─" * 58 + "│")
    for sid, cfg in MCP_SERVERS.items():
        connected = cfg["conn"] is not None
        status = "🟢 已连接" if connected else "⚪ 未连接"
        print(f"│  {sid}  {status}  工具数={len(cfg['tools'])}")
        print(f"│    {cfg['description']}")
        if connected and cfg["tools"]:
            print(f"│    工具: {', '.join(t['name'] for t in cfg['tools'])}")
    print("└" + "─" * 58 + "┘")


# ============ 7d. Hooks 层(本期核心新增)============
# Hooks = 工具执行前后的"检查点"。Before Hook 可以拦截/改写工具调用,
# After Hook 只观察、不干预(审计)。两者都挂在 chat_with_tools 的工具循环上:
#
#   LLM 发起 tool_call
#        │
#        ▼
#   ┌─ Before 钩子链(按注册顺序)────────────────┐
#   │ 1. sensitive_word_guard  拦截型:敏感词命中   │
#   │    且目标是"落盘/外发"类工具 → BLOCK        │
#   │ 2. sensitive_word_masker  改写型:其余工具    │
#   │    参数里的敏感词打码成 *** → 放行           │
#   └──────────────────────────────────────────┘
#        │ blocked? ── 是 → 不执行工具,把拦截原因
#        │                 作为工具结果还给 LLM
#        ▼ 否
#   execute_tool(可能已被改写的参数)
#        │
#        ▼
#   ┌─ After 钩子链(只观察)──────────────────────┐
#   │ 1. audit_logger  审计型:时间/工具/状态/     │
#   │    参数摘要/结果摘要/耗时 → audit_log.jsonl │
#   └──────────────────────────────────────────┘

# 敏感词表(教学演示用;生产中通常来自配置中心,可热更新)
SENSITIVE_WORDS = [
    "密码", "password", "passwd", "secret", "api_key", "apikey",
    "token", "密钥", "身份证", "银行卡", "card_number", "cvv",
]

# "落盘/外发"类工具 = 数据会离开本次对话(写文件 / 发消息 / 调外部服务器),
# 敏感词只对它们硬拦截;其余工具(纯计算/查询)只做打码改写
PROTECTED_TOOLS = {
    "save_user_profile",          # 写长期记忆文件
    "mcp_call_tool",              # 调外部 MCP 服务器
    "send_message",               # 队友间发消息
    "broadcast_message",          # 团队广播
    "dispatch_subagent",          # 派子代理(带任务描述出去)
    "dispatch_subagents_parallel",
}


def _scan_sensitive(text: str) -> list[str]:
    """扫描一段文本,返回命中的敏感词列表(大小写不敏感)。"""
    low = text.lower()
    return [w for w in SENSITIVE_WORDS if w.lower() in low]


def _mask_str(text: str) -> str:
    """把字符串里的敏感词替换成 ***(长度对齐,教学上更直观)。"""
    import re
    for word in SENSITIVE_WORDS:
        text = re.sub(re.escape(word), "*" * max(3, len(word)), text, flags=re.IGNORECASE)
    return text


def _mask_args(obj):
    """递归遍历参数(嵌套 dict/list/str),把所有字符串里的敏感词打码。"""
    if isinstance(obj, dict):
        return {k: _mask_args(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_args(v) for v in obj]
    if isinstance(obj, str):
        return _mask_str(obj)
    return obj


# ---- Before Hook 1:敏感词拦截(拦截型)----
def _hook_sensitive_guard(tool_name: str, arguments: dict) -> tuple[bool, dict, str]:
    """保护名单里的工具(落盘/外发),参数命中敏感词 → 直接拦截。

    返回 (blocked, new_args, reason):
    - blocked=True:工具不执行,reason 作为"拦截说明"返回给 LLM
    - blocked=False:放行(交给下一个 Hook)
    """
    if tool_name not in PROTECTED_TOOLS:
        return False, arguments, ""
    hits = _scan_sensitive(json.dumps(arguments, ensure_ascii=False))
    if not hits:
        return False, arguments, ""
    reason = (
        f"敏感词拦截:工具 {tool_name} 的参数中出现敏感词 {hits}。"
        f"该工具属于落盘/外发类(PROTECTED_TOOLS),已被 Before Hook "
        f"sensitive_word_guard 阻止执行。请勿保存或外发敏感信息。"
    )
    print(f"  [Hook 拦截] sensitive_word_guard: {tool_name} 参数命中 {hits} → 已阻断")
    return True, arguments, reason


# ---- Before Hook 2:敏感词打码(改写型)----
def _hook_sensitive_masker(tool_name: str, arguments: dict) -> tuple[bool, dict, str]:
    """非保护名单的工具,参数命中敏感词 → 打码改写后放行(演示"改写")。"""
    if tool_name in PROTECTED_TOOLS:
        return False, arguments, ""
    hits = _scan_sensitive(json.dumps(arguments, ensure_ascii=False))
    if not hits:
        return False, arguments, ""
    new_args = _mask_args(arguments)
    print(f"  [Hook 改写] sensitive_word_masker: {tool_name} 参数中 {hits} → 已打码为 ***")
    return False, new_args, f"参数中的敏感词 {hits} 已打码"


# ---- After Hook:操作审计(审计型)----
def _hook_audit_logger(tool_name: str, arguments: dict, result: str,
                       status: str, duration_ms: float) -> None:
    """每次工具调用(无论成功/被拦截/被改写)都追加一条 JSONL 审计记录。

    status 取值:
    - ok      正常执行
    - masked  执行了,但参数被 Before Hook 改写过
    - blocked 被 Before Hook 拦截,工具根本没执行
    """
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": tool_name,
        "status": status,
        "arguments": json.dumps(arguments, ensure_ascii=False)[:200],
        "result": (result or "")[:200],
        "duration_ms": round(duration_ms, 1),
    }
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---- Hooks 注册表(钩子链:按列表顺序依次执行)----
HOOKS = {
    "before": [
        {
            "name": "sensitive_word_guard",
            "type": "拦截(block)",
            "target": "PROTECTED_TOOLS(落盘/外发类)",
            "handler": _hook_sensitive_guard,
            "description": "敏感词硬拦截:命中即阻断,工具不执行",
        },
        {
            "name": "sensitive_word_masker",
            "type": "改写(rewrite)",
            "target": "其余工具",
            "handler": _hook_sensitive_masker,
            "description": "敏感词打码:参数中的敏感词替换为 *** 后放行",
        },
    ],
    "after": [
        {
            "name": "audit_logger",
            "type": "审计(audit)",
            "target": "所有工具调用",
            "handler": _hook_audit_logger,
            "description": "操作审计:每次调用(含被拦截的)写入 audit_log.jsonl",
        },
    ],
}


def run_before_hooks(tool_name: str, arguments: dict) -> tuple[bool, dict, str]:
    """依次执行 Before 钩子链。

    返回 (blocked, new_args, note):
    - blocked=True:被某个 Hook 拦截,调用方不应执行工具
    - new_args:可能被改写后的新参数(原参数不动,改写只影响本次执行)
    - note:拦截/改写说明(用于审计与日志)
    """
    args = arguments
    note = ""
    for hook in HOOKS["before"]:
        blocked, args, why = hook["handler"](tool_name, args)
        if why:
            note = why
        if blocked:
            return True, args, note
    return False, args, note


def run_after_hooks(tool_name: str, arguments: dict, result: str,
                    status: str, duration_ms: float) -> None:
    """依次执行 After 钩子链(只观察、不干预)。"""
    for hook in HOOKS["after"]:
        hook["handler"](tool_name, arguments, result, status, duration_ms)


# ---- Hooks 工具实现(暴露给 LLM 的 2 个) ----
def list_hooks() -> str:
    hooks = []
    for phase in ("before", "after"):
        for h in HOOKS[phase]:
            hooks.append({
                "phase": phase,
                "name": h["name"],
                "type": h["type"],
                "target": h["target"],
                "description": h["description"],
            })
    return json.dumps({
        "hooks": hooks,
        "sensitive_words": SENSITIVE_WORDS,
        "protected_tools": sorted(PROTECTED_TOOLS),
    }, ensure_ascii=False)


def view_audit_log(limit: int = 20) -> str:
    """读取审计日志最近 N 条(从后往前)。"""
    if not AUDIT_LOG_PATH.exists():
        return json.dumps({"audit_log": [], "note": "审计日志还没有记录"}, ensure_ascii=False)
    entries = []
    with open(AUDIT_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    recent = entries[-limit:]
    stats = {}
    for e in entries:
        stats[e.get("status", "?")] = stats.get(e.get("status", "?"), 0) + 1
    return json.dumps({
        "total_entries": len(entries),
        "stats": stats,
        "showing_last": len(recent),
        "audit_log": recent,
    }, ensure_ascii=False)


def _audit_count() -> int:
    """当前审计日志的条数(用于判断本轮是否有新的工具调用记录)。"""
    if not AUDIT_LOG_PATH.exists():
        return 0
    with open(AUDIT_LOG_PATH, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def print_hooks_panel():
    """打印 Hooks 面板(教学可视化)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  🛡️  Alex Hooks 面板(第 12 期新增)                        │")
    print("├" + "─" * 58 + "┤")
    print(f"│  Before 钩子链: {len(HOOKS['before'])} 个   After 钩子链: {len(HOOKS['after'])} 个")
    print("│" + "─" * 58 + "│")
    for phase in ("before", "after"):
        for h in HOOKS[phase]:
            print(f"│  [{phase:6s}] {h['name']}({h['type']})")
            print(f"│           适用: {h['target']}")
    print("│" + "─" * 58 + "│")
    print(f"│  敏感词表: {len(SENSITIVE_WORDS)} 个")
    print(f"│  保护名单: {len(PROTECTED_TOOLS)} 个工具(命中即拦截)")
    print("└" + "─" * 58 + "┘")


def print_audit_panel(limit: int = 10):
    """打印审计面板:统计 + 最近几条。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  📜 Alex 操作审计面板(第 12 期新增)                       │")
    print("├" + "─" * 58 + "┤")
    if not AUDIT_LOG_PATH.exists():
        print("│  审计日志为空(还没有任何工具调用记录)")
        print("└" + "─" * 58 + "┘")
        return
    entries = []
    with open(AUDIT_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    stats = {}
    for e in entries:
        stats[e.get("status", "?")] = stats.get(e.get("status", "?"), 0) + 1
    print(f"│  累计记录: {len(entries)} 条   状态分布: {stats}")
    print("│" + "─" * 58 + "│")
    icon = {"ok": "✅", "masked": "🖍 ", "blocked": "⛔"}
    for e in entries[-limit:]:
        print(f"│  {icon.get(e['status'], '•')} {e['ts']}  {e['tool']}  "
              f"[{e['status']}]  {e['duration_ms']}ms")
    print(f"│  (日志文件: {AUDIT_LOG_PATH.name})")
    print("└" + "─" * 58 + "┘")


# ============ 7e. 多模型路由层(本期核心新增)============
# 路由一图流:
#
#   用户消息 / 后台任务(compact / 子代理 / 队友)
#        │
#        ▼
#   ┌─ resolve_model ──────────────────────────────┐
#   │ manual 模式? → 用钉住的模型(switch_model 设的) │
#   │ auto 模式?   → route_model(messages):         │
#   │   ① 复杂关键词(审查/分析/设计/对比...)命中     │
#   │      或消息很长 → heavy 档(deepseek-reasoner)  │
#   │   ② 其余 → light 档(deepseek-chat,便宜)       │
#   │ 后台任务 → 固定 BACKGROUND_MODEL(light 档)    │
#   └──────────────────────────────────────────────┘
#        │ model_id
#        ▼
#   llm_call:MODEL_REGISTRY[model_id]["api_model"] → 真实端点
#        │
#        ▼
#   ┌─ log_usage(成本 telemetry)──────────────────┐
#   │ token 数 × 注册表单价 → 美元成本             │
#   │ 追加 usage_log.jsonl(时间/模型/任务/费用)    │
#   └──────────────────────────────────────────────┘
#
# 设计要点:所有 LLM 调用必须过 llm_call 这一个入口 —— 路由在前(选模型),
# 记账在后(算成本)。位置决定职责,和第 12 期 Hooks 的挂载哲学一脉相承。

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


# ============ 8. 工具定义(第 12 期 22 个 + 本期新增 3 个 = 25 个)============
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。当用户问'现在几点''今天日期'等问题时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "description": "目标时区,默认 Asia/Shanghai。"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "进行四则运算。当用户要求计算数学表达式时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "数学表达式,如 '17 * 23'"}
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "加载一个技能的完整内容。当任务与技能索引中某个技能相关时,"
                "先调用此工具拿到技能全文,再按技能内容回答。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名,如 weather、git-cheatsheet"}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_user_profile",
            "description": (
                "更新用户画像文件 templates/USER.md。当用户表达了稳定偏好、"
                "个人信息变更时调用。不要在每次对话后都调用,只在偏好真正变化时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "description": "要更新的章节名,如 '稳定偏好'"},
                    "new_content": {"type": "string", "description": "该章节的新内容(完整替换该章节)"},
                },
                "required": ["section", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "查询 Agent 记得的关于用户的信息。返回用户画像内容。"
                "当用户问'你还记得我吗''我之前说过什么'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "查询主题,可省略,返回全部画像。"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_create",
            "description": (
                "把一个模糊需求拆成可执行的子任务列表。"
                "当用户提出复杂任务(如'帮我准备周会''帮我做代码审查')时,"
                "先用此工具拆解成 3-7 个具体子任务,再逐个执行。"
                "所有任务初始状态为 pending。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "子任务内容列表,如 ['收集本周工作要点', '整理待讨论议题', '写周报草稿']",
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_update",
            "description": (
                "更新任务状态。状态可选:pending(待办)、in_progress(进行中)、completed(已完成)。"
                "约束 1(顺序):设为 in_progress 时,排在前面的任务必须全部 completed,不能跳过。"
                "约束 2(单 in_progress):同一时刻只能有一个任务处于 in_progress,"
                "设新的会自动把之前的 in_progress 退回 pending。"
                "工作流:按顺序逐个推进 #1→#2→#3,开始时 in_progress,完成后 completed,再做下一个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "要更新的任务 ID"},
                    "new_status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                        "description": "新状态",
                    },
                },
                "required": ["task_id", "new_status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_list",
            "description": (
                "查看当前任务列表及进度统计。返回各任务的状态和完成率。"
                "当你需要回顾当前进度、确认下一步做什么时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # ↓↓↓ 第 09 期:子代理工具 ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "dispatch_subagent",
            "description": (
                "派遣一个子代理执行特定任务。子代理有独立上下文(不复用主 Agent 对话历史)"
                "和工具白名单(只能用部分工具)。子代理是临时的,用完即弃。"
                "当任务需要某个专家角色(如代码审查、文档整理)且不需要跨会话保持状态时调用。"
                "可用类型:code_reviewer(代码审查员)、doc_writer(文档整理员)。"
                "注意:子代理不能用 todo、不能用 save_user_profile、不能派遣其他子代理。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_type": {
                        "type": "string",
                        "enum": ["code_reviewer", "doc_writer"],
                        "description": "子代理类型",
                    },
                    "task": {"type": "string", "description": "要交给子代理的任务描述"},
                },
                "required": ["agent_type", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_subagents_parallel",
            "description": (
                "并发派遣多个子代理。所有子代理同时开始执行,总耗时约等于最慢的那个。"
                "当需要多个专家同时工作时调用(如'审查代码 + 整理文档')。"
                "tasks 是一个列表,每项包含 agent_type 和 task。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_type": {
                                    "type": "string",
                                    "enum": ["code_reviewer", "doc_writer"],
                                    "description": "子代理类型",
                                },
                                "task": {"type": "string", "description": "任务描述"},
                            },
                            "required": ["agent_type", "task"],
                        },
                        "description": "子代理任务列表",
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_subagents",
            "description": "查看可用的子代理类型及其工具白名单。当不确定有哪些子代理时调用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # ↓↓↓ 第 10 期新增:Agent Team 工具(本期沿用) ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "team_wake",
            "description": (
                "唤醒一个持久队友执行任务。队友的对话历史和 inbox 跨唤醒保持"
                "(不像子代理用完即弃)。唤醒时,队友会自动读取 inbox 里的未读消息。"
                "适用场景:需要多个角色多轮协作(如前端+后端协作开发功能)时使用。"
                "可用成员:frontend_dev(前端开发)、backend_dev(后端开发)。"
                "队友之间可以通过 send_message / broadcast_message 互相通信。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "member_id": {
                        "type": "string",
                        "enum": ["frontend_dev", "backend_dev"],
                        "description": "要唤醒的团队成员 ID",
                    },
                    "task": {"type": "string", "description": "交给队友的任务描述"},
                },
                "required": ["member_id", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "给指定队友的 inbox 发送一条消息。消息进入对方 inbox,"
                "对方下次被 team_wake 唤醒时会自动看到。"
                "主 Agent 和队友都能调用(队友调用时发送者为自己)。"
                "适用场景:前端队友需要接口定义时给后端队友发消息。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_member": {
                        "type": "string",
                        "enum": ["frontend_dev", "backend_dev"],
                        "description": "接收消息的队友 ID",
                    },
                    "content": {"type": "string", "description": "消息内容"},
                },
                "required": ["to_member", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "broadcast_message",
            "description": (
                "向所有队友的 inbox 广播一条消息。一条消息同时进入所有队友的 inbox。"
                "适用场景:需求变更通知、全局进度同步等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "广播消息内容"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_inbox",
            "description": (
                "读取某个队友的 inbox 消息(不消费,仅查看)。"
                "当你想了解队友收到了哪些消息时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "member_id": {
                        "type": "string",
                        "enum": ["frontend_dev", "backend_dev"],
                        "description": "要查看 inbox 的队友 ID",
                    },
                },
                "required": ["member_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_team",
            "description": (
                "查看团队成员列表及其状态(inbox 未读消息数、唤醒次数、是否有持久上下文)。"
                "当不确定团队里有哪些成员时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # ↓↓↓ 第 11 期新增:MCP 工具(本期沿用) ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "list_mcp_servers",
            "description": (
                "查看有哪些 MCP 服务器可连接(名称、描述、连接状态、工具数量)。"
                "当用户提到需要外部工具能力(如查仓库、查数据库)时,先调用此工具看看有什么服务器。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_connect",
            "description": (
                "连接一个 MCP 服务器(启动服务器子进程 + initialize 握手 + 拉取工具清单)。"
                "调用 MCP 服务器上的工具之前,一般先连接它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "enum": ["repo"],
                        "description": "MCP 服务器 ID,如 repo",
                    },
                },
                "required": ["server_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_list_tools",
            "description": (
                "列出某个 MCP 服务器提供的工具(名称、描述、参数 schema)。"
                "调用 MCP 工具前,先看它叫什么名字、要传什么参数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "MCP 服务器 ID,如 repo",
                    },
                },
                "required": ["server_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_call_tool",
            "description": (
                "调用 MCP 服务器上的一个工具。这是访问外部工具服务器的统一入口:"
                "传服务器 ID + 工具名 + 参数 JSON,返回工具的文本结果。"
                "用法示例:mcp_call_tool('repo', 'get_repository_info', "
                "{\"owner\": \"deepseek-ai\", \"repo\": \"DeepSeek-V3\"})"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "MCP 服务器 ID,如 repo",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "服务器上的工具名,如 get_repository_info",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "工具参数(JSON 对象),具体结构见 mcp_list_tools 的 inputSchema",
                    },
                },
                "required": ["server_id", "tool_name", "arguments"],
            },
        },
    },
    # ↓↓↓ 本期新增:Hooks 工具(第 12 期) ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "list_hooks",
            "description": (
                "查看当前注册的 Hooks(工具调用前后的钩子链):每个钩子的阶段"
                "(before/after)、名称、类型(拦截/改写/审计)、适用范围,以及敏感词表和保护工具名单。"
                "当用户想知道'你的工具调用有什么安全机制'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_audit_log",
            "description": (
                "查看操作审计日志(最近 N 条):每次工具调用的 时间/工具名/状态"
                "(ok 正常、masked 参数被打码、blocked 被拦截)/参数摘要/结果摘要/耗时,"
                "以及整体统计。当用户想回放'Agent 刚才都做了什么'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "返回最近多少条,默认 20",
                    },
                },
                "required": [],
            },
        },
    },
    # ↓↓↓ 本期新增:多模型路由工具(第 13 期) ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "list_models",
            "description": (
                "查看多模型注册表:每个模型的 档位(light 轻量/heavy 重型)/"
                "输入输出单价(美元每百万 token)/强项/底层 API 模型,"
                "以及当前路由模式(auto 自动/manual 手动钉死)和最近的路由决策记录。"
                "当用户问'你都有哪些模型可用''现在用的是哪个模型'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_model",
            "description": (
                "切换路由模式或手动指定模型。三种用法:"
                "① switch_model(mode='auto') 恢复自动路由(按任务复杂度选档);"
                "② switch_model(mode='manual', model_id='claude-sonnet') 钉死某个模型;"
                "③ switch_model(mode='auto', task='background') 临时指定后台任务用轻量模型。"
                "当用户说'用便宜点的模型''接下来都用 claude 回答'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "manual"],
                        "description": "路由模式:auto=自动按复杂度选档,manual=手动钉死模型",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "manual 模式下要钉死的模型 ID,如 deepseek-chat/claude-sonnet/local-qwen",
                    },
                    "task": {
                        "type": "string",
                        "description": "临时指定某类任务用的模型,如 background(后台任务)",
                    },
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cost_report",
            "description": (
                "查看成本 telemetry 报告:每个模型消耗的 token 数和费用(美元),"
                "按任务类别(main 主对话/compact 压缩/subagent 子代理/team 团队)分组统计,"
                "给出最贵的一次调用和省钱建议。当用户问'这次对话花了多少钱'"
                "'哪个模型最贵'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


# ============ 9. 工具实现(第 12 期 22 个 + 本期新增 3 个)============
_ALLOWED_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的常量: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
        return op(_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARYOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
        return op(_safe_eval(node.operand))
    raise ValueError(f"不支持的表达式类型: {type(node).__name__}")


def execute_tool(name: str, arguments: dict) -> str:
    if name == "get_current_time":
        tz = arguments.get("timezone", "Asia/Shanghai")
        now = datetime.now()
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return json.dumps({
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": tz,
            "weekday": weekdays[now.weekday()],
        }, ensure_ascii=False)

    if name == "calculate":
        expr = arguments.get("expression", "")
        try:
            tree = ast.parse(expr, mode="eval")
            result = _safe_eval(tree.body)
            return json.dumps({"expression": expr, "result": result}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"计算失败: {e}", "expression": expr}, ensure_ascii=False)

    if name == "load_skill":
        skill_name = arguments.get("name", "")
        skill_md = SKILLS_DIR / skill_name / "SKILL.md"
        if not skill_md.exists():
            available = ", ".join(s["name"] for s in load_skills_index())
            return json.dumps({
                "error": f"技能不存在: {skill_name}",
                "available": available,
            }, ensure_ascii=False)
        return json.dumps({
            "skill": skill_name,
            "content": skill_md.read_text(encoding="utf-8"),
        }, ensure_ascii=False)

    if name == "save_user_profile":
        section = arguments.get("section", "")
        new_content = arguments.get("new_content", "")
        if not USER_PATH.exists():
            return json.dumps({"error": "USER.md 不存在"}, ensure_ascii=False)
        text = USER_PATH.read_text(encoding="utf-8")
        lines = text.splitlines()
        out = []
        in_target = False
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(f"## {section}"):
                out.append(line)
                out.append("")
                out.append(new_content)
                out.append("")
                in_target = True
                replaced = True
                continue
            if in_target and line.startswith("## "):
                in_target = False
            if not in_target:
                out.append(line)
        if not replaced:
            out.append("")
            out.append(f"## {section}")
            out.append("")
            out.append(new_content)
        USER_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
        return json.dumps({"saved": section, "path": str(USER_PATH)}, ensure_ascii=False)

    if name == "recall_memory":
        topic = arguments.get("topic", "").strip()
        if not USER_PATH.exists():
            return json.dumps({"error": "USER.md 不存在"}, ensure_ascii=False)
        text = USER_PATH.read_text(encoding="utf-8")
        if not topic:
            return json.dumps({"profile": text}, ensure_ascii=False)
        lines = text.splitlines()
        matched = []
        in_match = False
        for line in lines:
            if line.startswith("## "):
                in_match = topic in line
            if in_match:
                matched.append(line)
        if not matched:
            return json.dumps({"topic": topic, "found": False, "profile": text}, ensure_ascii=False)
        return json.dumps({
            "topic": topic,
            "found": True,
            "excerpt": "\n".join(matched),
        }, ensure_ascii=False)

    # ↓↓↓ 第 08 期:TodoList 工具 ↓↓↓
    if name == "todo_create":
        tasks = arguments.get("tasks", [])
        if not tasks or not isinstance(tasks, list):
            return json.dumps({"error": "tasks 必须是非空列表"}, ensure_ascii=False)
        return todo_create(tasks)

    if name == "todo_update":
        task_id = arguments.get("task_id")
        new_status = arguments.get("new_status", "")
        if task_id is None:
            return json.dumps({"error": "task_id 必填"}, ensure_ascii=False)
        return todo_update(int(task_id), new_status)

    if name == "todo_list":
        return todo_list()

    # ↓↓↓ 第 09 期:Subagent 工具 ↓↓↓
    if name == "dispatch_subagent":
        agent_type = arguments.get("agent_type", "")
        task = arguments.get("task", "")
        if not agent_type or not task:
            return json.dumps({"error": "agent_type 和 task 必填"}, ensure_ascii=False)
        return dispatch_subagent(agent_type, task)

    if name == "dispatch_subagents_parallel":
        tasks = arguments.get("tasks", [])
        return dispatch_subagents_parallel(tasks)

    if name == "list_subagents":
        return list_subagents()

    # ↓↓↓ 第 10 期新增:Agent Team 工具(本期沿用) ↓↓↓
    if name == "team_wake":
        member_id = arguments.get("member_id", "")
        task = arguments.get("task", "")
        if not member_id or not task:
            return json.dumps({"error": "member_id 和 task 必填"}, ensure_ascii=False)
        return team_wake(member_id, task)

    if name == "send_message":
        to_member = arguments.get("to_member", "")
        content = arguments.get("content", "")
        if not to_member or not content:
            return json.dumps({"error": "to_member 和 content 必填"}, ensure_ascii=False)
        return send_message(to_member, content)

    if name == "broadcast_message":
        content = arguments.get("content", "")
        if not content:
            return json.dumps({"error": "content 必填"}, ensure_ascii=False)
        return broadcast_message(content)

    if name == "read_inbox":
        member_id = arguments.get("member_id", "")
        if not member_id:
            return json.dumps({"error": "member_id 必填"}, ensure_ascii=False)
        return read_inbox(member_id)

    if name == "list_team":
        return list_team()

    # ↓↓↓ 第 11 期新增:MCP 工具(本期沿用) ↓↓↓
    if name == "list_mcp_servers":
        return list_mcp_servers()

    if name == "mcp_connect":
        server_id = arguments.get("server_id", "")
        if not server_id:
            return json.dumps({"error": "server_id 必填"}, ensure_ascii=False)
        return mcp_connect(server_id)

    if name == "mcp_list_tools":
        server_id = arguments.get("server_id", "")
        if not server_id:
            return json.dumps({"error": "server_id 必填"}, ensure_ascii=False)
        return mcp_list_tools(server_id)

    if name == "mcp_call_tool":
        server_id = arguments.get("server_id", "")
        tool_name = arguments.get("tool_name", "")
        mcp_args = arguments.get("arguments", {})
        if not server_id or not tool_name:
            return json.dumps({"error": "server_id 和 tool_name 必填"}, ensure_ascii=False)
        if not isinstance(mcp_args, dict):
            return json.dumps({"error": "arguments 必须是 JSON 对象"}, ensure_ascii=False)
        return mcp_call_tool(server_id, tool_name, mcp_args)

    # ↓↓↓ 本期新增:Hooks 工具(第 12 期,本期沿用) ↓↓↓
    if name == "list_hooks":
        return list_hooks()

    if name == "view_audit_log":
        limit = arguments.get("limit", 20)
        if not isinstance(limit, int) or limit <= 0:
            limit = 20
        return view_audit_log(limit)

    # ↓↓↓ 本期新增:多模型路由工具(第 13 期) ↓↓↓
    if name == "list_models":
        return list_models()

    if name == "switch_model":
        mode = arguments.get("mode", "")
        if mode not in ("auto", "manual"):
            return json.dumps(
                {"error": "mode 必须是 auto 或 manual"}, ensure_ascii=False
            )
        model_id = arguments.get("model_id") or None
        task = arguments.get("task") or None
        return switch_model(mode=mode, model_id=model_id, task=task)

    if name == "cost_report":
        return cost_report()

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


# ============ 10. 工具循环(第 13 期改:LLM 调用走 llm_call 统一入口)============
def chat_with_tools(messages: list[dict], session_id: str | None = None) -> tuple[str, list[dict], int, int]:
    """带工具调用的对话。

    事件溯源:每条 assistant(tool_calls) / tool / assistant(回答)
    在 append 到 messages 后立即 persist_one 落盘,产生即持久化。

    第 12 期:每次工具调用先过 Before 钩子链(拦截/改写),
    执行(或被拦截)后再过 After 钩子链(审计落盘)。

    第 13 期:LLM 调用从直接 client.chat.completions.create(model=MODEL)
    改为走 llm_call(messages, task="main", tools=TOOLS) ——
    每一轮都过路由器(选模型)+ 记账器(算成本)。
    """
    total_prompt = 0
    total_completion = 0

    while True:
        response, model_id = llm_call(messages, task="main", tools=TOOLS)
        msg = response.choices[0].message
        total_prompt += response.usage.prompt_tokens
        total_completion += response.usage.completion_tokens

        if msg.tool_calls:
            assistant_msg = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            append_and_persist(messages, assistant_msg, session_id)
            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)
                print(f"  [工具调用] {fn_name}({fn_args})")
                # ↓↓↓ 本期新增:Before 钩子链(拦截 / 改写,第 12 期) ↓↓↓
                blocked, fn_args, hook_note = run_before_hooks(fn_name, fn_args)
                if blocked:
                    status = "blocked"
                    duration_ms = 0.0
                    result = json.dumps({
                        "error": "工具调用被 Before Hook 拦截,未执行",
                        "hook": "sensitive_word_guard",
                        "reason": hook_note,
                        "hint": "请向用户说明无法保存/外发该敏感信息,可建议脱敏后再试。",
                    }, ensure_ascii=False)
                else:
                    status = "masked" if hook_note else "ok"
                    t0 = time.time()
                    result = execute_tool(fn_name, fn_args)
                    duration_ms = (time.time() - t0) * 1000
                display = result if len(result) <= 80 else result[:80] + "..."
                print(f"  [工具结果] {display}")
                # ↓↓↓ 本期新增:After 钩子链(审计落盘,第 12 期) ↓↓↓
                run_after_hooks(fn_name, fn_args, result, status, duration_ms)
                append_and_persist(messages, {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }, session_id)
            continue

        append_and_persist(messages,
                           {"role": "assistant", "content": msg.content},
                           session_id)
        return msg.content, messages, total_prompt, total_completion


def trim_history(messages: list[dict]) -> list[dict]:
    """会话内上下文限制。超过 MAX_ROUNDS 轮时截断最早的完整回合。"""
    has_system = messages and messages[0]["role"] == "system"
    system_msg = [messages[0]] if has_system else []
    convo = messages[1:] if has_system else messages[:]

    user_positions = [i for i, m in enumerate(convo) if m["role"] == "user"]
    if len(user_positions) <= MAX_ROUNDS:
        return messages

    cut_idx = user_positions[-MAX_ROUNDS]
    return system_msg + convo[cut_idx:]


# ============ 11. 记忆可视化面板(与第 07-09 期一致)============
def print_memory_dashboard():
    """启动时打印 Alex 现在"记得"什么。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  🧠 Alex 记忆面板(第 07 期)                            │")
    print("├" + "─" * 58 + "┤")

    profile = load_user_profile()
    if profile:
        sections = []
        for line in profile.splitlines():
            if line.startswith("## "):
                sections.append(line.replace("## ", "").strip())
        print(f"│  [长期记忆] USER.md 加载成功({len(profile)} 字符)        │")
        for s in sections[:4]:
            short = s[:48] + "..." if len(s) > 48 else s
            print(f"│    • {short:<52}│")
    else:
        print("│  [长期记忆] USER.md 不存在,首次启动                  │")

    print("│" + "─" * 58 + "│")

    raw_count = count_raw_history_lines()
    print(f"│  [原始历史] raw_history.jsonl: {raw_count} 条记录(累计)        │")

    sessions = []
    if SESSIONS_PATH.exists():
        try:
            sessions = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    if sessions:
        last = sessions[-1]
        print(f"│  [上次会话] {last['ended_at']} 轮数={last['rounds']} "
              f"token={last['total_tokens']:<6}│")
    else:
        print("│  [上次会话] 无(首次启动)                              │")

    print("└" + "─" * 58 + "┘")
    print()


# ============ 12. 主流程 ============
def main():
    print("=" * 60)
    print("第 13 期:多模型路由 —— 自动选模型 + 成本 telemetry")
    print("=" * 60)
    print(f"当前模型池: {', '.join(MODEL_REGISTRY.keys())}")
    print(f"路由模式: {ROUTER_STATE['mode']}(简单→{TIER_DEFAULT['light']},复杂→{TIER_DEFAULT['heavy']})")
    print(f"后台任务(compact/子代理/队友)固定: {BACKGROUND_MODEL}")
    print(f"API 地址: {client.base_url}(教学:全部走 DeepSeek 端点,按注册表模拟计费)")
    print(f"上下文上限: {MAX_ROUNDS} 轮(超出自动截断)")
    print(f"压缩阈值: {COMPACT_THRESHOLD_ROUNDS} 轮(超出自动 compact)")
    print(f"已装工具: {', '.join(t['function']['name'] for t in TOOLS)}")

    skills_index = load_skills_index()
    print(f"技能索引: {len(skills_index)} 个")
    print(f"子代理类型: {', '.join(SUBAGENT_TYPES.keys())}")
    print(f"团队成员: {', '.join(TEAM_MEMBERS.keys())}")

    # 加载三层记忆
    soul = load_system_prompt()
    user_profile = load_user_profile()
    skills_prompt = build_skills_prompt(skills_index)

    # 拼装 system prompt:人设 + 技能索引 + 用户画像 + TodoList 指南 + Subagent 指南 + Team 指南
    system_prompt = soul
    if skills_prompt:
        system_prompt += "\n" + skills_prompt
    if user_profile:
        system_prompt += "\n\n# 用户画像(长期记忆)\n\n" + user_profile
        print(f"[人设] {SOUL_PATH}")
        print(f"[用户画像] {USER_PATH}({len(user_profile)} 字符)")
    else:
        print("[用户画像] 未加载")

    # TodoList 工作指南(从模板文件加载,注入 system prompt)
    if TODO_GUIDE_PATH.exists():
        todo_guide = "\n" + TODO_GUIDE_PATH.read_text(encoding="utf-8")
        system_prompt += todo_guide
        print(f"[TodoList 指南] {TODO_GUIDE_PATH}({len(todo_guide)} 字符)")
    else:
        print(f"[TodoList 指南] 未加载:{TODO_GUIDE_PATH} 不存在")

    # Subagent 工作指南(内联,注入 system prompt)
    subagent_guide = (
        "\n\n## 子代理使用指南\n"
        "你可以通过 dispatch_subagent 派遣单个子代理,或通过 dispatch_subagents_parallel 并发派遣多个。\n"
        "决策流程:\n"
        "1. 当任务涉及多个专家角色(如代码审查 + 文档整理)时,用 dispatch_subagents_parallel 并发派遣\n"
        "2. 当任务只需要单个专家时,用 dispatch_subagent 派遣一个\n"
        "3. 不确定有哪些子代理时,用 list_subagents 查看\n"
        "重要:子代理有独立上下文和工具白名单,它们的对话不会出现在你的上下文里。\n"
        "你收到的是子代理的最终结果(文本),不是它们的对话过程。\n"
    )
    system_prompt += subagent_guide
    print(f"[Subagent 指南] 内联注入({len(subagent_guide)} 字符)")

    # Agent Team 工作指南(内联,注入 system prompt)
    team_guide = (
        "\n\n## Agent Team 使用指南\n"
        "你可以通过 team_wake 唤醒持久队友。队友的对话历史和 inbox 跨唤醒保持。\n"
        "队友之间可以通过 send_message 互发消息,通过 broadcast_message 广播消息。\n"
        "决策流程:\n"
        "1. 当任务需要多个角色多轮协作时(如前端+后端合作开发),用 team_wake 逐个唤醒\n"
        "2. 当需要给队友传话时,用 send_message 发到对方 inbox\n"
        "3. 当需要通知所有人时,用 broadcast_message 广播\n"
        "4. 当需要查看队友状态时,用 list_team\n"
        "5. 当需要查看队友收到的消息时,用 read_inbox\n"
        "关键差异:子agent 是临时的(用完即弃),队友是持久的(上下文跨唤醒保持)。\n"
        "使用模式:唤醒前端 → 前端给后端发消息要接口 → 唤醒后端 → 后端回复 → 再唤醒前端对接。\n"
    )
    system_prompt += team_guide
    print(f"[Team 指南] 内联注入({len(team_guide)} 字符)")

    # MCP 使用指南(内联,注入 system prompt)
    mcp_guide = (
        "\n\n## MCP 使用指南\n"
        "你可以通过 MCP(Model Context Protocol)连接外部工具服务器。"
        "MCP 服务器是独立进程,通过 stdio(JSON-RPC 2.0)与你通信,它的工具不写在你自己的代码里。\n"
        "决策流程:\n"
        "1. 先用 list_mcp_servers 查看有哪些可用的 MCP 服务器\n"
        "2. 用 mcp_connect 连接服务器(握手 + 拉取工具清单)\n"
        "3. 不确定工具名/参数时,用 mcp_list_tools 查看服务器提供哪些工具及其参数 schema\n"
        "4. 用 mcp_call_tool(server_id, tool_name, arguments) 调用服务器上的工具\n"
        "与内置工具的区别:内置工具(get_current_time、calculate)写死在你的代码里;"
        "MCP 工具在外部服务器进程里,连上就能用,服务器升级/更换你一行代码都不用改。\n"
        "典型场景:查 GitHub 仓库、查数据库、查文档——只要对方提供 MCP 服务器,你就能接。\n"
    )
    system_prompt += mcp_guide
    print(f"[MCP 指南] 内联注入({len(mcp_guide)} 字符)")

    # Hooks 使用指南(内联,注入 system prompt)
    hooks_guide = (
        "\n\n## Hooks 使用指南\n"
        "你的每一次工具调用都会经过 Hooks(钩子)检查:\n"
        "1. Before 钩子链(执行前):sensitive_word_guard 会拦截'落盘/外发'类工具"
        "(save_user_profile / mcp_call_tool / send_message 等)参数中的敏感词"
        "(密码、密钥、身份证等);sensitive_word_masker 会把其他工具参数中的敏感词打码。\n"
        "2. After 钩子链(执行后):audit_logger 把每次调用(含被拦截的)写入审计日志。\n"
        "决策流程:\n"
        "1. 用户想知道你的安全机制时,用 list_hooks 查看钩子链\n"
        "2. 用户想回放'你刚才做了什么'时,用 view_audit_log 查看审计日志\n"
        "3. 工具返回'被 Before Hook 拦截'的错误时,不要重试原参数,应向用户说明"
        "该信息属于敏感数据、无法保存或外发,并建议脱敏后再试\n"
        "重要:被拦截不是故障,是安全机制在工作。审计日志里的 blocked 状态就是证据。\n"
    )
    system_prompt += hooks_guide
    print(f"[Hooks 指南] 内联注入({len(hooks_guide)} 字符)")

    # 多模型路由指南(第 13 期内联,注入 system prompt)
    router_guide = (
        "\n\n## 多模型路由指南\n"
        "你可以使用多个模型,由路由器自动决定每次调用用哪个:\n"
        f"- light 档({TIER_DEFAULT['light']}):便宜、快,处理简单问答和后台任务\n"
        f"- heavy 档({TIER_DEFAULT['heavy']}):深度推理,处理复杂分析(命中'审查/分析/设计/对比'等关键词时自动升级)\n"
        f"- 其他模型(claude-sonnet/local-qwen)也已在注册表里,可手动钉死\n"
        "- 后台任务(compact 压缩、子代理、队友)固定走 light 档,这是省钱策略\n"
        "决策流程:\n"
        "1. 用户问'你有哪些模型/现在用哪个'时,用 list_models 查看注册表和路由状态\n"
        "2. 用户说'换便宜点的模型'/'接下来都用 claude'时,用 switch_model 切换"
        "(mode='auto' 恢复自动,mode='manual'+model_id 钉死)\n"
        "3. 用户问'花了多少钱'/'哪个模型最贵'时,用 cost_report 查看成本 telemetry\n"
        "重要:路由对你透明 —— 你的每次调用都已被记账,成本数据是真实的,可以放心引用 cost_report 的数字。\n"
    )
    system_prompt += router_guide
    print(f"[路由指南] 内联注入({len(router_guide)} 字符)")

    # ============ 记忆面板 ============
    print_memory_dashboard()

    # TodoList 面板(初始为空)
    print_todo_panel()

    # Subagent 面板(初始为空)
    print_subagent_panel()

    # Team 面板(初始为空)
    print_team_panel()

    # MCP 面板(初始为未连接)
    print_mcp_panel()

    # Hooks 面板(钩子链 + 敏感词表)
    print_hooks_panel()

    # 审计面板(可能为空,也可能有历史记录)
    print_audit_panel()

    # 模型路由面板(第 13 期新增:模型池 + 路由模式 + 注册表)
    print_model_panel()

    # 成本面板(第 13 期新增:历史账单可能为空)
    print_cost_panel()

    # 演示 session_id
    demo_session_id = f"demo-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # ============ 自动演示 1:简单任务 → 路由到 light 档(便宜) ============
    print('\n[自动演示 1] 简单任务:路由器应选 light 档(deepseek-chat,便宜):\n')

    demo_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "现在几点了?顺便用 calculate 算一下 17 * 23,把两个结果都告诉我。"
        )},
    ]
    try:
        answer, demo_messages, p, c = chat_with_tools(demo_messages, demo_session_id)
        log_session(demo_session_id, 1, p, c)
        print(f"\n[Alex] {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
        print_model_panel()
    except Exception as e:
        print(f"[出错] {e}")

    print("\n" + "─" * 60)

    # ============ 自动演示 2:复杂任务 → 路由到 heavy 档 + 成本报表 ============
    print('\n[自动演示 2] 复杂任务(命中"分析"关键词→heavy 档)+ 查成本报表:\n')
    demo_messages2 = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "帮我做两件事:\n"
            "1. 分析一下:像我们这样的 Agent,什么时候该用便宜模型、"
            "什么时候该上贵模型?给我一个判断标准\n"
            "2. 用 cost_report 看看刚才到现在一共花了多少钱,哪个模型最贵,"
            "把数字汇报给我"
        )},
    ]
    try:
        answer, demo_messages2, p, c = chat_with_tools(demo_messages2, demo_session_id)
        log_session(demo_session_id, 1, p, c)
        print(f"\n[Alex] {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
        print_model_panel()
        print_cost_panel()
    except Exception as e:
        print(f"[出错] {e}")

    print("\n" + "─" * 60)

    # ============ 自由对话(累积式) ============
    print("\n现在进入自由对话(Alex + 人设 + 技能库 + 长期记忆 + TodoList + 子代理 + Agent Team + MCP + Hooks + 多模型路由)")
    print("输入 quit 退出 / reset 清空 / history 历史 / memory 记忆面板")
    print("     soul 人设 / skills 技能索引 / tools 工具列表 / todo 任务面板")
    print("     subagents 子代理面板 / dispatch 手动派遣")
    print("     team 团队面板 / wake 手动唤醒队友")
    print("     mcp MCP 面板(外部工具服务器)")
    print("     hooks 钩子链面板 / audit 审计日志面板")
    print("     models 模型路由面板 / cost 成本面板(telemetry)")
    print("←/→ 光标,↑/↓ 历史,Ctrl-C 作废本行")
    print("=" * 60)

    base = os.environ.get("AGENT_SESSION")
    if base and os.environ.get("AGENT_SESSION_RESET") == "1":
        import uuid
        session_id = f"{base}-{datetime.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"
        print(f"[session] 模式 3(项目内开新轮): {session_id!r}")
    elif base:
        session_id = base
        print(f"[session] 模式 2(跨启动接续): {session_id!r}")
    else:
        session_id = f"auto-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        print(f"[session] 模式 1(本次独立): {session_id!r}"
              f"\n           想跨启动接续?设 AGENT_SESSION=<name>")

    print_team_panel()

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    total_prompt = 0
    total_completion = 0
    cli_history = InMemoryHistory()
    rounds = 0

    while True:
        try:
            user_input = prompt("\n你: ", history=cli_history).strip()
        except (EOFError, KeyboardInterrupt):
            print("  (本行作废)")
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            log_session(session_id, rounds, total_prompt, total_completion)
            shutdown_mcp()  # 关掉所有 MCP 子进程,不留孤儿进程
            print(f"\n[本次会话] 轮数={rounds}  prompt={total_prompt}  "
                  f"completion={total_completion}  total={total_prompt + total_completion}")
            print(f"[raw_history] {RAW_HISTORY_PATH}(每轮对话已实时落盘)")
            print(f"[audit_log] {AUDIT_LOG_PATH}(每次工具调用/拦截已实时审计)")
            print(f"[usage_log] {USAGE_LOG_PATH}(每次 LLM 调用成本已实时记账)")
            print("再见!")
            break
        if user_input.lower() == "reset":
            messages = [{"role": "system", "content": system_prompt}]
            total_prompt = 0
            total_completion = 0
            rounds = 0
            print("[提示] 会话内历史已清空,长期记忆和技能索引保留。原始历史在 raw_history.jsonl 里。")
            continue
        if user_input.lower() == "history":
            print(f"[会话历史] 共 {len(messages)} 条消息:")
            for idx, m in enumerate(messages):
                role = m["role"]
                content = m.get("content", "")
                if m.get("tool_calls") and not content:
                    calls = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
                    content = f"[tool_calls: {calls}]"
                elif not content:
                    content = "(无文本)"
                content = content[:50] + ("..." if len(content) > 50 else "")
                print(f"  {idx:2d}. [{role:9s}] {content}")
            continue
        if user_input.lower() == "soul":
            print(f"[人设 + 技能 + 用户画像 + TodoList 指南 + Subagent 指南 + Team 指南 + MCP 指南 + Hooks 指南]:\n{system_prompt[:800]}...\n")
            continue
        if user_input.lower() == "skills":
            print(f"[技能索引] {len(skills_index)} 个(全文未加载):")
            for s in skills_index:
                print(f"  - {s['name']}: {s['description']}")
            continue
        if user_input.lower() == "tools":
            print(f"[工具] 共 {len(TOOLS)} 个:")
            for t in TOOLS:
                fn = t["function"]
                print(f"  - {fn['name']}: {fn['description'][:60]}...")
            continue
        if user_input.lower() == "memory":
            print_memory_dashboard()
            recent = read_recent_raw_history(5)
            if recent:
                print(f"[最近 {len(recent)} 条原始历史]")
                for r in recent:
                    print(f"  {r['ts']} [{r['role']}] {r['content'][:60]}")
            continue
        if user_input.lower() == "todo":
            print_todo_panel()
            continue
        if user_input.lower() == "subagents":
            print_subagent_panel()
            continue
        if user_input.lower() == "dispatch":
            print("[子代理类型]")
            for name, config in SUBAGENT_TYPES.items():
                print(f"  - {name}: {config['description']}")
                print(f"    允许工具: {', '.join(config['allowed_tools'])}")
            print("\n[提示] 问 Alex 一个问题,如'帮我审查这段代码并整理文档',它会自己派子代理。")
            continue
        if user_input.lower() == "team":
            print_team_panel()
            continue
        if user_input.lower() == "mcp":
            print_mcp_panel()
            print("\n[MCP 服务器]")
            for sid, cfg in MCP_SERVERS.items():
                connected = "已连接" if cfg["conn"] is not None else "未连接"
                print(f"  - {sid}: {cfg['description']}")
                print(f"    状态={connected}  工具数={len(cfg['tools'])}")
            print("\n[提示] 问 Alex:'帮我通过 MCP 查一下 deepseek-ai 的仓库信息',它会自己连服务器、调工具。")
            continue
        if user_input.lower() == "hooks":
            print_hooks_panel()
            print("\n[提示] 问 Alex:'你有哪些安全钩子?',它会调用 list_hooks 汇报钩子链。")
            continue
        if user_input.lower() == "audit":
            print_audit_panel()
            print("\n[提示] 问 Alex:'把审计日志最近 10 条读给我听',它会调用 view_audit_log。")
            continue
        if user_input.lower() == "models":
            print_model_panel()
            print("\n[提示] 问 Alex:'你有哪些模型?现在用的是哪个?',它会调用 list_models。")
            print("[提示] 说'接下来都用 claude-sonnet 回答',它会调用 switch_model 钉死模型。")
            continue
        if user_input.lower() == "cost":
            print_cost_panel()
            print("\n[提示] 问 Alex:'这次对话花了多少钱?',它会调用 cost_report 汇报账单。")
            continue
        if user_input.lower() == "wake":
            print("[团队成员]")
            for mid, config in TEAM_MEMBERS.items():
                inbox_n = len(config["inbox"])
                wakes = config["wake_count"]
                has_ctx = "有" if config["messages"] is not None else "无"
                print(f"  - {mid}: {config['description']}")
                print(f"    inbox={inbox_n} 未读  唤醒={wakes} 次  上下文={has_ctx}")
                print(f"    允许工具: {', '.join(config['allowed_tools'])}")
            print("\n[提示] 问 Alex 一个问题,如'让前端和后端协作开发登录功能',它会自己组队。")
            continue
        if not user_input:
            continue

        append_and_persist(messages, {"role": "user", "content": user_input}, session_id)
        rounds += 1

        messages = trim_history(messages)
        if rounds > COMPACT_THRESHOLD_ROUNDS and sum(1 for m in messages if m["role"] == "user") > COMPACT_THRESHOLD_ROUNDS:
            print("[compact] 触发自动压缩...")
            messages = compact_history(messages)

        audit_before = _audit_count()
        try:
            answer, messages, p, c = chat_with_tools(messages, session_id)
            total_prompt += p
            total_completion += c
            print(f"Alex: {answer}")
            print(f"[token] 本次 prompt={p}  completion={c}  "
                  f"累计 total={total_prompt + total_completion}  轮数={rounds}")
            # 每轮对话后如果有子代理活动,打印面板
            if _subagent_runs:
                print_subagent_panel()
            if _todo_list:
                print_todo_panel()
            # 如果团队有活动,打印团队面板
            team_active = any(
                cfg["wake_count"] > 0 or cfg["inbox"]
                for cfg in TEAM_MEMBERS.values()
            )
            if team_active:
                print_team_panel()
            # 如果有 MCP 服务器连接活动,打印 MCP 面板
            mcp_active = any(cfg["conn"] is not None for cfg in MCP_SERVERS.values())
            if mcp_active:
                print_mcp_panel()
            # 如果本轮有新的审计记录(工具被调用/拦截),打印审计面板
            if _audit_count() > audit_before:
                print_audit_panel(limit=5)
            # 第 13 期:每次 LLM 调用都有路由决策,打印模型路由面板(含当次模型)
            if ROUTER_STATE["history"]:
                print_model_panel()
        except Exception as e:
            messages.pop()
            rounds -= 1
            print(f"[出错] {e}")
            continue

        log_session(session_id, rounds, total_prompt, total_completion)


if __name__ == "__main__":
    main()