"""memory.py — 记忆层(上下文管理 + 持久化 + 用户画像 + 可视化面板)

[第 06 期] 用户画像读写:save_user_profile / recall_memory(USER.md)
[第 07 期] 三层记忆 + 记忆面板:print_memory_dashboard
  1. 原始历史 raw_history.jsonl(append-only,事件溯源)
  2. 会话元数据 sessions.json(upsert)
  3. 上下文压缩 compact_history(LLM 摘要化早期对话)
[第 07 期] 会话内截断:trim_history(超过 MAX_ROUNDS 轮时截断最早回合)
"""
import json
from datetime import datetime

from .config import (
    client, MODEL,
    MEMORY_DIR, RAW_HISTORY_PATH, SESSIONS_PATH, COMPACT_PROMPT_PATH,
    COMPACT_THRESHOLD_ROUNDS, MAX_ROUNDS, USER_PATH,
)
from .prompt import load_user_profile


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


# ============ 用户画像读写(第 06 期) ============

def save_user_profile(section: str, new_content: str) -> str:
    """更新 USER.md 中指定 section 的内容,不存在则追加。"""
    if not USER_PATH.exists():
        return json.dumps({"error": "USER.md 不存在"}, ensure_ascii=False)
    text = USER_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    out = []
    in_target = False
    replaced = False
    for line in lines:
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


def recall_memory(topic: str = "") -> str:
    """从 USER.md 读取用户画像,可按 topic 过滤 section。"""
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


# ============ 记忆可视化面板(第 07 期) ============

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
