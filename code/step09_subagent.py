#!/usr/bin/env python3
"""step09_subagent.py — 第 09 期:子代理 Subagent

原理一句话:主 Agent 通过 dispatch_subagent / dispatch_subagents_parallel
派遣子代理(独立上下文 + 工具白名单 + 并发执行),子代理用自己的 messages
和受限工具集独立完成任务,结果回传给主 Agent = Subagent 机制。

本期目标:
1. 解决第 08 期埋的伏笔:Alex 有了 TodoList 能拆任务、标进度,但遇到"需要两个
   专家同时看"的场景(如代码审查 + 文档整理)还是串行排队——一个做完才做下一个
2. 引入子代理机制 + 三个新增工具:
   - dispatch_subagent           — 派遣单个子代理(同步,独立上下文 + 工具白名单)
   - dispatch_subagents_parallel — 并发派遣多个子代理(ThreadPoolExecutor 真并发)
   - list_subagents              — 查看可用子代理类型及权限
3. 三个核心概念:
   - 独立上下文:子代理有自己的 messages,不复用主 Agent 的对话历史
     → 避免"代码审查"的上下文污染"文档整理"的上下文
   - 工具白名单:子代理只能用 allowed_tools 中的工具
     → LLM 看不到未授权工具 = 物理隔离,不是"提醒"
   - 并发派遣:多个子代理并行工作(ThreadPoolExecutor)
     → 代码审查 + 文档整理同时跑,总耗时 ≈ max(两者),不是两者之和
4. 两个预定义子代理类型:
   - code_reviewer(代码审查员)— allowed_tools: [get_current_time, calculate, load_skill]
   - doc_writer(文档整理员)  — allowed_tools: [get_current_time, calculate, load_skill]
5. 关键约束(工具白名单层面强制):
   - 子代理不能用 todo_create / todo_update / todo_list(只有主 Agent 管任务)
   - 子代理不能用 save_user_profile / recall_memory(只有主 Agent 管记忆)
   - 子代理不能用 dispatch_subagent(禁止递归派遣,避免无限嵌套)
6. 演示场景:并发派遣"代码审查员" + "文档整理员"同时工作

累积式:step09 = step08 + Subagent
(保留 step08 的三层记忆、技能层、工具循环、TodoList 全部逻辑,
 只新增 Subagent 层 + 3 个工具)

运行:
    python code/step09_subagent.py
"""
import os
import json
import ast
import operator
import time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

load_dotenv()

# ============ 1. 初始化客户端(与第 01-08 期一致)============
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

MAX_ROUNDS = 10  # 会话内短期记忆上限(轮数)

# ============ 2. 路径(与第 07-08 期一致)============
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
MEMORY_DIR = Path(__file__).parent.parent / "memory"
SOUL_PATH = TEMPLATES_DIR / "SOUL.md"
USER_PATH = TEMPLATES_DIR / "USER.md"
COMPACT_PROMPT_PATH = TEMPLATES_DIR / "compact_prompt.md"
TODO_GUIDE_PATH = TEMPLATES_DIR / "todo_guide.md"
RAW_HISTORY_PATH = MEMORY_DIR / "raw_history.jsonl"
SESSIONS_PATH = MEMORY_DIR / "sessions.json"

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
            "path": str(skill_md),
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
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": compact_prompt},
                {"role": "user", "content": f"请压缩以下对话历史:\n\n{compact_input}"},
            ],
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


# ============ 7. Subagent 层(本期核心新增)============
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
    total_prompt = 0
    total_completion = 0

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=sub_messages,
            tools=sub_tools,
            tool_choice="auto",
            max_tokens=2048,
        )
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
    print("│  🔀 Alex 子代理面板(第 09 期新增)                     │")
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


# ============ 8. 工具定义(第 08 期 8 个 + 本期新增 3 个 = 11 个)============
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
    # ↓↓↓ 本期新增:子代理工具 ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "dispatch_subagent",
            "description": (
                "派遣一个子代理执行特定任务。子代理有独立上下文(不复用主 Agent 对话历史)"
                "和工具白名单(只能用部分工具)。"
                "当任务需要某个专家角色(如代码审查、文档整理)时调用。"
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
]


# ============ 9. 工具实现(第 08 期 8 个 + 本期新增 3 个)============
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

    # ↓↓↓ 本期新增:Subagent 工具 ↓↓↓
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

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


# ============ 10. 工具循环(与第 08 期完全一致,Subagent 工具也复用它)============
def chat_with_tools(messages: list[dict], session_id: str | None = None) -> tuple[str, list[dict], int, int]:
    """带工具调用的对话。

    事件溯源:每条 assistant(tool_calls) / tool / assistant(回答)
    在 append 到 messages 后立即 persist_one 落盘,产生即持久化。
    """
    total_prompt = 0
    total_completion = 0

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=4096,
        )
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
                result = execute_tool(fn_name, fn_args)
                display = result if len(result) <= 80 else result[:80] + "..."
                print(f"  [工具结果] {display}")
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


# ============ 11. 记忆可视化面板(与第 07-08 期一致)============
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
    print("第 09 期:子代理 Subagent —— 让 Alex 学会派活给别人")
    print("=" * 60)
    print(f"当前模型: {MODEL}")
    print(f"API 地址: {client.base_url}")
    print(f"上下文上限: {MAX_ROUNDS} 轮(超出自动截断)")
    print(f"压缩阈值: {COMPACT_THRESHOLD_ROUNDS} 轮(超出自动 compact)")
    print(f"已装工具: {', '.join(t['function']['name'] for t in TOOLS)}")

    skills_index = load_skills_index()
    print(f"技能索引: {len(skills_index)} 个")
    print(f"子代理类型: {', '.join(SUBAGENT_TYPES.keys())}")

    # 加载三层记忆
    soul = load_system_prompt()
    user_profile = load_user_profile()
    skills_prompt = build_skills_prompt(skills_index)

    # 拼装 system prompt:人设 + 技能索引 + 用户画像 + TodoList 指南 + Subagent 指南
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

    # ============ 记忆面板 ============
    print_memory_dashboard()

    # TodoList 面板(初始为空)
    print_todo_panel()

    # Subagent 面板(初始为空)
    print_subagent_panel()

    # 演示 session_id
    demo_session_id = f"demo-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # ============ 自动演示:并发派遣"代码审查员"+"文档整理员" ============
    demo_code = '''def fetch_user(uid):
    users = {1: "Alice", 2: "Bob"}
    return users[uid]

def calculate_price(qty, unit_price):
    total = qty * unit_price
    discount = 0.1
    return total - total * discount'''

    print('[自动演示] 并发派遣"代码审查员" + "文档整理员":\n')
    print(f'  代码:\n{demo_code}\n')

    demo_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"请帮我做两件事:\n"
            f"1. 审查这段代码,指出问题和改进建议\n"
            f"2. 把这段代码整理成一份技术文档(含函数说明和用法示例)\n\n"
            f"代码:\n```python\n{demo_code}\n```"
        )},
    ]
    try:
        answer, demo_messages, p, c = chat_with_tools(demo_messages, demo_session_id)
        log_session(demo_session_id, 1, p, c)
        print(f"\n[Alex] {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
        print_subagent_panel()
    except Exception as e:
        print(f"[出错] {e}")

    print("\n" + "─" * 60)

    # ============ 自动演示 2:单独派遣一个子代理 ============
    print('\n[自动演示 2] 单独派遣"文档整理员":\n')
    demo_messages2 = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "帮我把这句话整理成规范的技术文档格式:'这个函数用来算价格,输入数量和单价,返回打折后的总价'"},
    ]
    try:
        answer, demo_messages2, p, c = chat_with_tools(demo_messages2, demo_session_id)
        log_session(demo_session_id, 1, p, c)
        print(f"\n[Alex] {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
        print_subagent_panel()
    except Exception as e:
        print(f"[出错] {e}")

    print("\n" + "─" * 60)

    # ============ 自由对话(累积式) ============
    print("\n现在进入自由对话(Alex + 人设 + 技能库 + 长期记忆 + TodoList + 子代理)")
    print("输入 quit 退出 / reset 清空 / history 历史 / memory 记忆面板")
    print("     soul 人设 / skills 技能索引 / tools 工具列表 / todo 任务面板")
    print("     subagents 子代理面板 / dispatch 手动派遣")
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

    print_subagent_panel()

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
            print(f"\n[本次会话] 轮数={rounds}  prompt={total_prompt}  "
                  f"completion={total_completion}  total={total_prompt + total_completion}")
            print(f"[raw_history] {RAW_HISTORY_PATH}(每轮对话已实时落盘)")
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
            print(f"[人设 + 技能 + 用户画像 + TodoList 指南 + Subagent 指南]:\n{system_prompt[:800]}...\n")
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
        if not user_input:
            continue

        append_and_persist(messages, {"role": "user", "content": user_input}, session_id)
        rounds += 1

        messages = trim_history(messages)
        if rounds > COMPACT_THRESHOLD_ROUNDS and sum(1 for m in messages if m["role"] == "user") > COMPACT_THRESHOLD_ROUNDS:
            print("[compact] 触发自动压缩...")
            messages = compact_history(messages)

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
        except Exception as e:
            messages.pop()
            rounds -= 1
            print(f"[出错] {e}")
            continue

        log_session(session_id, rounds, total_prompt, total_completion)


if __name__ == "__main__":
    main()
