#!/usr/bin/env python3
"""step07_memory.py — 第 07 期:记忆系统

本期目标:
1. 解决第 03-06 期埋的伏笔:Alex 重启就忘,history 只在会话内有效
2. 引入三层记忆 + 两个新增工具:
   - 原始历史(raw_history.jsonl)—— append-only,事件产生即落盘
   - 长期记忆 / 用户画像(templates/USER.md)—— 稳定偏好,启动注入 system prompt
   - compact 压缩 —— 历史超长时压缩成摘要,腾上下文
   - save_user_profile(更新用户画像) + recall_memory(查询用户画像)
3. 记忆可视化面板:启动时打印"Alex 现在记得什么"
4. 事件溯源纪律:所有对话事件通过 append_and_persist 统一入口同步落盘

累积式:step07 = step06 + memory/ + USER.md + compact
(保留 step06 的工具循环、技能层全部逻辑,只新增记忆层 + 2 个工具)

运行:
    python code/step07_memory.py
"""
import os
import json
import ast
import operator
import time
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

load_dotenv()

# ============ 1. 初始化客户端(与第 01-06 期一致)============
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

MAX_ROUNDS = 10  # 会话内短期记忆上限(轮数)

# ============ 2. 路径(本期新增)============
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
MEMORY_DIR = Path(__file__).parent.parent / "memory"
SOUL_PATH = TEMPLATES_DIR / "SOUL.md"
USER_PATH = TEMPLATES_DIR / "USER.md"
COMPACT_PROMPT_PATH = TEMPLATES_DIR / "compact_prompt.md"
RAW_HISTORY_PATH = MEMORY_DIR / "raw_history.jsonl"
SESSIONS_PATH = MEMORY_DIR / "sessions.json"

# 压缩触发阈值(轮数);超过后自动调用 compact
COMPACT_THRESHOLD_ROUNDS = 8

# 配置约束:触发阈值必须 < 保留上限。否则会出现"通过触发检查但切窗口不够"
# 的尴尬区间(如阈值=8、上限=10 时 9~10 轮会进入函数又退出)。
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
    """读取 templates/USER.md 作为长期记忆 / 用户画像。

    这是第 07 期的关键新增:用户画像在启动时被注入 system prompt,
    Agent 能"记得"上次说过的偏好,即使重启也不会丢。
    """
    if not USER_PATH.exists():
        return ""
    return USER_PATH.read_text(encoding="utf-8")


# ============ 4. 技能层(与第 06 期完全一致)============
SKILLS_DIR = Path(__file__).parent.parent / "skills"


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


# ============ 5. 记忆层(本期核心新增)============
def load_compact_prompt() -> str:
    """读取 compact 提示词模板。"""
    if not COMPACT_PROMPT_PATH.exists():
        return "请把上面的对话压缩成简洁的第三人称摘要,保留关键事实、偏好和未完成任务。"
    return COMPACT_PROMPT_PATH.read_text(encoding="utf-8")


def append_raw_history(session_id: str, role: str, content: str, extra: dict | None = None):
    """原始历史落盘(append-only JSONL)。

    关键:这是 raw history,记录完整对话,不裁剪不压缩,
    用于训练(未来)、审计、回溯。即使 compact 把它从上下文清掉了,
    文件里仍然在。
    """
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
    """把一条 message 折成可落盘的 content 字符串。

    优先用 content;若 assistant 带 tool_calls 则记成 [tool_calls: ...] 占位
    (避免 tool_calls 这种结构化字段直接序列化污染 JSONL)。
    """
    content = m.get("content") or ""
    if m.get("tool_calls"):
        calls = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
        content = f"[tool_calls: {calls}]"
    if not content:
        content = f"({m.get('role', '?')}: 空内容)"
    return content


def persist_one(m: dict, session_id: str, source: str, max_chars: int = 500):
    """单条消息立即落盘。事件溯源:append 即写,不攒不延迟。

    source 字段写"这条消息从哪来"——produced(对话产生)/quit(退出汇总)
    区分用途,方便审计。

    system 消息不写(它是模板,不是对话事件)。
    空 content 不写(没信息量)。
    失败不抛——日志审计不能影响对话流程。
    """
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
    """唯一的"对话事件"入口:append 到 messages + 立即落盘。

    所有对话事件(用户输入/assistant/tool/最终回答)都走这里,
    而不是直接 messages.append(...)+persist_one(...)分两行写——
    保证"messages 加了一条,raw_history 也加了一条"原子绑定,绝不漏写。

    返回是否落盘成功(便于上层判断)。
    """
    messages.append(msg)
    if session_id:
        return persist_one(msg, session_id, source=source)
    return False


def log_session(session_id: str, rounds: int, prompt_tokens: int, completion_tokens: int):
    """会话元数据 upsert 到 sessions.json。

    upsert 语义:
    - 首次调用(session_id 不在文件里)→ append 新记录
    - 后续调用(session_id 已存在)→ 更新同一条(覆盖轮数/token)

    每轮对话后调用一次,保证 Ctrl-C / 进程崩溃也有"上一轮"的快照。
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    sessions = []
    if SESSIONS_PATH.exists():
        try:
            sessions = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            sessions = []

    # upsert:找到同 session_id 的记录就更新,找不到就 append
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
            sessions[i] = record  # 同一进程内的 upsert
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
    """压缩早期对话:用 LLM 把历史 messages 摘要化,替换掉早期的几轮。

    触发条件:user 轮数 > COMPACT_THRESHOLD_ROUNDS(默认 8)。
    保留:system + 最近 MAX_ROUNDS 个 user 之后的全部消息(含它的 tool/assistant)。
    替换:前面的早期消息 → 一条摘要 message(role=assistant,标注 [compact summary])。

    与 trim 的关系:trim 和 compact 共用同一个保留上限 MAX_ROUNDS(默认 10)。
    区别只是 trim 无脑截断、compact 用摘要换更早的可见性。
    触发时机不同,但保留目标一致。

    设计原则:本函数只重组 messages,不碰 raw_history。
    持久化由 append_and_persist 在事件产生时已经做完——到这里只是"重排 + 摘要"。

    前置条件:启动期已 assert COMPACT_THRESHOLD_ROUNDS < MAX_ROUNDS(见顶部)。
    """
    if not messages or messages[0]["role"] != "system":
        return messages
    system_msg = messages[0]
    convo = messages[1:]
    user_rounds = sum(1 for m in convo if m["role"] == "user")
    if user_rounds <= COMPACT_THRESHOLD_ROUNDS:
        return messages  # 未到压缩阈值,不动

    user_positions = [i for i, m in enumerate(convo) if m["role"] == "user"]
    # 运行期不变量:即使配置已 assert,数据本身仍可能被外部破坏(落盘损坏、
    # 重放异常等),必须再查一遍——配置是逻辑保证,这里是数据保证。
    if len(user_positions) < MAX_ROUNDS:
        return messages  # 数据不足,不动(优雅兜底,不崩)
    # 取倒数第 MAX_ROUNDS 个 user 的位置作为分界,该位置之前压成摘要、之后保留
    boundary = user_positions[-MAX_ROUNDS]
    to_compress = convo[:boundary]
    keep = convo[boundary:]

    if not to_compress:
        return messages

    # 把要压缩的消息拼成文本,调 LLM 生成摘要
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

    # 组装新 messages:system + 摘要 + 近期对话
    new_messages = [system_msg]
    new_messages.append({
        "role": "assistant",
        "content": f"[compact summary] 下面是早期对话的摘要,用于节省上下文:\n\n{summary}",
    })
    new_messages.extend(keep)
    return new_messages


# ============ 6. 工具定义(第 06 期 3 个 + 本期新增 2 个)============
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。当用户问'现在几点''今天日期''今天星期几'等问题时调用。",
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
        # ↓↓↓ 本期新增:更新用户画像 ↓↓↓
        "type": "function",
        "function": {
            "name": "save_user_profile",
            "description": (
                "更新用户画像文件 templates/USER.md。当用户表达了稳定偏好、"
                "个人信息变更、或明确表态'我以后都用 X'时调用此工具。"
                "不要在每次对话后都调用,只在偏好真正变化时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "要更新的章节名,如 '稳定偏好'、'基础信息'",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "该章节的新内容(完整替换该章节)",
                    },
                },
                "required": ["section", "new_content"],
            },
        },
    },
    {
        # ↓↓↓ 本期新增:记忆状态查询(教学用,模型可自查)↓↓↓
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
                    "topic": {
                        "type": "string",
                        "description": "查询主题,如 '我的 Python 版本'、'我的角色'。可省略,返回全部画像。",
                    }
                },
                "required": [],
            },
        },
    },
]


# ============ 7. 工具实现(第 06 期 3 个 + 本期新增 2 个)============
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
        # 更新 USER.md:替换指定章节的内容
        section = arguments.get("section", "")
        new_content = arguments.get("new_content", "")
        if not USER_PATH.exists():
            return json.dumps({"error": "USER.md 不存在"}, ensure_ascii=False)
        text = USER_PATH.read_text(encoding="utf-8")
        # 章节格式: "## 章节名" 开头,到下一个 "## " 或文件尾
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
            # 章节不存在 → 追加到末尾
            out.append("")
            out.append(f"## {section}")
            out.append("")
            out.append(new_content)
        USER_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
        return json.dumps({"saved": section, "path": str(USER_PATH)}, ensure_ascii=False)

    if name == "recall_memory":
        # 返回用户画像全文(可按主题过滤)
        topic = arguments.get("topic", "").strip()
        if not USER_PATH.exists():
            return json.dumps({"error": "USER.md 不存在"}, ensure_ascii=False)
        text = USER_PATH.read_text(encoding="utf-8")
        if not topic:
            return json.dumps({"profile": text}, ensure_ascii=False)
        # 简单过滤:包含主题的章节
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

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


# ============ 8. 工具循环(与第 06 期完全一致,记忆工具也复用它)============
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
            max_tokens=1000,
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
    """会话内上下文限制。超过 MAX_ROUNDS 轮时截断最早的完整回合。

    设计原则:本函数只重组 messages,不碰 raw_history。
    持久化由 append_and_persist 在事件产生时已经做完——到这里只是"切片"。
    被丢弃的消息在 raw_history 里仍然完整保留(append-only 的承诺)。
    """
    has_system = messages and messages[0]["role"] == "system"
    system_msg = [messages[0]] if has_system else []
    convo = messages[1:] if has_system else messages[:]

    user_positions = [i for i, m in enumerate(convo) if m["role"] == "user"]
    if len(user_positions) <= MAX_ROUNDS:
        return messages  # 未到阈值,不动

    cut_idx = user_positions[-MAX_ROUNDS]
    return system_msg + convo[cut_idx:]


# ============ 9. 记忆可视化面板(本期教学亮点)============
def print_memory_dashboard():
    """启动时打印 Alex 现在"记得"什么。直观对比 raw history / user profile。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  🧠 Alex 记忆面板(第 07 期新增)                        │")
    print("├" + "─" * 58 + "┤")

    # 1. 用户画像
    profile = load_user_profile()
    if profile:
        # 抽取前 3 个 ## 章节
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

    # 2. 原始历史
    raw_count = count_raw_history_lines()
    print(f"│  [原始历史] raw_history.jsonl: {raw_count} 条记录(累计)        │")

    # 3. 会话元数据
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


# ============ 10. 主流程 ============
def main():
    print("=" * 60)
    print("第 07 期:记忆系统 —— 让 Alex 跨会话也记得你")
    print("=" * 60)
    print(f"当前模型: {MODEL}")
    print(f"API 地址: {client.base_url}")
    print(f"上下文上限: {MAX_ROUNDS} 轮(超出自动截断)")
    print(f"压缩阈值: {COMPACT_THRESHOLD_ROUNDS} 轮(超出自动 compact)")
    print(f"已装工具: {', '.join(t['function']['name'] for t in TOOLS)}")

    skills_index = load_skills_index()
    print(f"技能索引: {len(skills_index)} 个")

    # 加载三层记忆
    soul = load_system_prompt()
    user_profile = load_user_profile()
    skills_prompt = build_skills_prompt(skills_index)

    # 拼装 system prompt:人设 + 技能索引 + 用户画像
    system_prompt = soul
    if skills_prompt:
        system_prompt += "\n" + skills_prompt
    if user_profile:
        system_prompt += "\n\n# 用户画像(长期记忆)\n\n" + user_profile
        print(f"[人设] {SOUL_PATH}")
        print(f"[用户画像] {USER_PATH}({len(user_profile)} 字符)")
    else:
        print("[用户画像] 未加载")

    # ============ 记忆面板 ============
    print_memory_dashboard()

    # 演示阶段也用 session_id,保证 demo 期间的对话事件也落盘(事件溯源承诺)
    demo_session_id = f"demo-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


    # ============ 自动演示 1:recall_memory 验证长期记忆 ============
    print('[自动演示 1] 让 Alex 调用 recall_memory 查"我之前说过什么":\n')
    demo_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Alex,我之前跟你说过我是做什么的吗?"},
    ]
    try:
        answer, demo_messages, p, c = chat_with_tools(demo_messages, demo_session_id)
        #演示1 session
        log_session(demo_session_id, 1, p, c)
        print(f"\n[Alex] {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
    except Exception as e:
        print(f"[出错] {e}")
        return

    print("\n" + "─" * 60)

    # ============ 自动演示 2:save_user_profile 更新画像 ============
    print('\n[自动演示 2] 让 Alex 用 save_user_profile 记录新偏好 "我开始用 uv 了":\n')
    demo_messages2 = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Alex 记一下:我开始用 uv 替代 pip 了,以后所有项目都用 uv。"},
    ]
    try:
        answer, demo_messages2, p, c = chat_with_tools(demo_messages2, demo_session_id)
        #演示2 session
        log_session(demo_session_id, 1, p, c)
        print(f"\n[Alex] {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
        # 验证:再次读 USER.md 看是否更新
        if "uv" in load_user_profile():
            print(f"[验证] USER.md 已包含 'uv',长期记忆写入成功 ✓")
        else:
            print(f"[验证] USER.md 未包含 'uv',写入未生效 ✗")
    except Exception as e:
        print(f"[出错] {e}")
        return

    print("\n" + "─" * 60)

    # ============ 自由对话(累积式) ============
    print("\n现在进入自由对话(Alex + 人设 + 技能库 + 长期记忆)")
    print("输入 quit 退出 / reset 清空 / history 历史 / memory 记忆面板")
    print("     soul 人设 / skills 技能索引 / tools 工具列表 / recall 查记忆")
    print("←/→ 光标,↑/↓ 历史,Ctrl-C 作废本行")
    print("=" * 60)

    # session_id 三种模式(从环境变量读):
    #   默认(auto):"auto-YYYYMMDD-HHMMSS",每次启动天然独立(模式 1)
    #   AGENT_SESSION=<name>:固定名,跨启动同 ID(模式 2,适合多项目并发)
    #   AGENT_SESSION=<name> + AGENT_SESSION_RESET=1:同名字但 UUID 后缀
    #     (模式 3,适合"项目内明确开新会话")
    #
    # ⚠️ 重要:本期 session_id 只用作 raw_history.jsonl 里的审计标签,
    #    不实现会话续接。具体说:
    #      - 不影响 USER.md(长期记忆全局共享,跟 session_id 无关)
    #      - 不影响 raw_history.jsonl 的写入路径(单一全局文件,所有 session
    #        往里追加,只靠 session 字段区分)
    #      - 不影响 system prompt(启动时只读 USER.md,不管 session_id)
    #      - 不影响 compact / trim(只看 messages 长度)
    #    唯一的实用价值:grep raw_history 能按 session 名筛出"同一段工作"。
    #
    #    想真做会话续接需要:
    #      1. 按 session 隔离 raw_history(写到 memory/sessions/<id>.jsonl)
    #      2. 启动时按 session 加载最近 N 条注入 messages
    #      3. 按 session 维护独立 profile
    #
    # 选哪种?对照:
    #   - 单人随便聊  → 默认(模式 1)
    #   - 跨启动接续  → 模式 2(给个固定名字,仅审计层面同组)
    #   - 项目里开新轮 → 模式 3
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

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    total_prompt = 0
    total_completion = 0
    cli_history = InMemoryHistory()
    rounds = 0

    # 注意:无需 atexit 兜底——所有对话事件都在 append_and_persist 里
    # 同步落盘了,Ctrl-C / 异常退出也不丢。

    while True:
        try:
            user_input = prompt("\n你: ", history=cli_history).strip()
        except (EOFError, KeyboardInterrupt):
            print("  (本行作废)")
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            # 退出前:同步最终会话元数据(每轮已 upsert,这里只是覆盖最后状态)
            log_session(session_id, rounds, total_prompt, total_completion)
            print(f"\n[本次会话] 轮数={rounds}  prompt={total_prompt}  "
                  f"completion={total_completion}  total={total_prompt + total_completion}")
            print(f"[raw_history] {RAW_HISTORY_PATH}(每轮对话已实时落盘)")
            print("再见!")
            break
        if user_input.lower() == "reset":
            # raw_history 不需要再写——append 时已全部入库。清空内存即可
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
            print(f"[人设 + 技能 + 用户画像]:\n{system_prompt[:800]}...\n")
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
        if user_input.lower() == "recall":
            print("[提示] 问 Alex 一个问题,如'我之前说过什么偏好?',它会自己调 recall_memory。")
            continue
        if not user_input:
            continue

        append_and_persist(messages, {"role": "user", "content": user_input}, session_id)
        rounds += 1

        # 先截断,再 compact(都只重组 messages,落盘已在 append 时完成)
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
        except Exception as e:
            messages.pop()
            rounds -= 1
            print(f"[出错] {e}")
            continue

        # 每轮对话后 upsert 会话元数据(崩了也至少有"上次轮"的快照)
        log_session(session_id, rounds, total_prompt, total_completion)


if __name__ == "__main__":
    main()
