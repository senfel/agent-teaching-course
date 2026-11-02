"""stream.py — 流式工具循环(第 15 期核心新增)

把第 05 期 chat_with_tools(工具循环)从"一次性返回回答"改成"边生成边吐块",
第 15 期的 Web UI 依赖它做 SSE(Server-Sent Events)流式响应。

与 agent.py 的 chat_with_tools 是一对"非流式 / 流式"孪生:
  - 两者都跑同一套工具循环:LLM → 有 tool_calls? → 执行工具 → 回灌 → 继续
  - 两者都挂 Hooks(第 12 期)+ 走 llm_call(第 13 期路由与记账)
  - 唯一区别:stream_chat_with_tools 把 LLM 的每个增量文本 yield 出去,
    让前端能"一个字一个字"看到 Alex 在写;工具状态也作为事件 yield,
    前端可以实时显示"正在调用 xx 工具…"

事件协议(每个 yield 是一个 dict,由 web.py 序列化成 SSE 的 data 行):
  {"event": "delta",       "text": "...", "model": "deepseek-chat"}  # 文本增量
  {"event": "tool_call",   "name": "...", "arguments": {...}}         # 准备调工具
  {"event": "tool_result", "name": "...", "result": "..."}            # 工具结果
  {"event": "done",        "text": "..."}                             # 本轮结束(无工具调用时)

生成器耗尽 = 本轮对话结束。append_and_persist 与 chat_with_tools 保持
事件溯源一致:产生的 assistant/tool 消息"产生即落盘"。
"""
import json
import time

from .model_router import llm_call_stream
from .memory import append_and_persist
from .tools import TOOLS, execute_tool
from .hooks import run_before_hooks, run_after_hooks


def _run_one_tool(tool_call_id: str, fn_name: str, raw_args: str):
    """执行一个工具(含 Before/After Hook),返回可展示的结果。

    与 agent.py 里的工具执行逻辑保持一致,只是把"print"换成了"yield 调用方展示"。
    """
    try:
        fn_args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        fn_args = {}

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

    run_after_hooks(fn_name, fn_args, result, status, duration_ms)
    return fn_args, result


def stream_chat_with_tools(messages: list[dict], session_id: str | None = None):
    """流式版工具循环。生成器:每个 yield 是一个事件 dict(见模块 docstring)。

    循环结构与非流式 chat_with_tools 完全一致:
      while True: 一次 LLM 调用(流式)→ 累积分片 →
        有 tool_calls → 逐个执行工具(过 Hooks)→ 回灌 → continue
        无 tool_calls → 落地回答 → yield done → 结束
    """
    while True:
        content_parts: list[str] = []
        # index → {id, name, arguments}:流式里 tool_call 的字段是分片到的,要按 index 攒
        tool_calls: dict[int, dict] = {}

        for delta, model_id in llm_call_stream(messages, task="main", tools=TOOLS):
            if delta.content:
                content_parts.append(delta.content)
                yield {"event": "delta", "text": delta.content, "model": model_id}

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = tool_calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["arguments"] += tc.function.arguments

        content = "".join(content_parts).strip()

        if tool_calls:
            # 先把 assistant 的 tool_calls 消息落盘(事件溯源)
            ordered = [tool_calls[i] for i in sorted(tool_calls)]
            append_and_persist(messages, {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": slot["id"],
                        "type": "function",
                        "function": {"name": slot["name"], "arguments": slot["arguments"]},
                    }
                    for slot in ordered
                ],
            }, session_id)

            for slot in ordered:
                yield {"event": "tool_call", "name": slot["name"], "arguments": slot["arguments"]}
                fn_args, result = _run_one_tool(slot["id"], slot["name"], slot["arguments"])
                yield {"event": "tool_result", "name": slot["name"], "result": result}
                append_and_persist(messages, {
                    "role": "tool",
                    "tool_call_id": slot["id"],
                    "content": result,
                }, session_id)
            continue

        append_and_persist(messages, {"role": "assistant", "content": content}, session_id)
        yield {"event": "done", "text": content}
        return