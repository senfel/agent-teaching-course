"""hooks.py — Hooks 层(第 12 期新增)

Hooks = 工具执行前后的"检查点"。Before Hook 可以拦截(block)或改写(rewrite)
即将执行的工具调用,After Hook 只观察、不干预(审计)。
给 Agent 的每一次"动手"装上安检门 + 行车记录仪。

[第 12 期] 本模块新增:
  - SENSITIVE_WORDS   敏感词表(密码/密钥/身份证 ...)
  - PROTECTED_TOOLS   保护名单(落盘/外发类工具,命中敏感词即硬拦截)
  - 3 个钩子          sensitive_word_guard(拦截)/ sensitive_word_masker(改写)/ audit_logger(审计)
  - HOOKS             钩子注册表(before / after 两条链)
  - run_before_hooks / run_after_hooks   钩子链执行入口(agent.py 的工具循环里挂载)
  - 2 个工具实现      list_hooks / view_audit_log(暴露给 LLM)
  - print_hooks_panel / print_audit_panel  面板(教学可视化)

挂在 chat_with_tools 的工具循环上:

  LLM 发起 tool_call
       │
       ▼
  ┌─ Before 钩子链(按注册顺序)────────────────┐
  │ 1. sensitive_word_guard  拦截型:敏感词命中   │
  │    且目标是"落盘/外发"类工具 → BLOCK        │
  │ 2. sensitive_word_masker  改写型:其余工具    │
  │    参数里的敏感词打码成 *** → 放行           │
  └──────────────────────────────────────────┘
       │ blocked? ── 是 → 不执行工具,把拦截原因
       │                 作为工具结果还给 LLM
       ▼ 否
  execute_tool(可能已被改写的参数)
       │
       ▼
  ┌─ After 钩子链(只观察)──────────────────────┐
  │ 1. audit_logger  审计型:时间/工具/状态/     │
  │    参数摘要/结果摘要/耗时 → audit_log.jsonl │
  └──────────────────────────────────────────┘
"""
import json
import re
from datetime import datetime

from .config import MEMORY_DIR, AUDIT_LOG_PATH

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
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
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
    with AUDIT_LOG_PATH.open(encoding="utf-8") as f:
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
    with AUDIT_LOG_PATH.open(encoding="utf-8") as f:
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
    with AUDIT_LOG_PATH.open(encoding="utf-8") as f:
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
