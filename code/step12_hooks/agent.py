"""agent.py — 工具循环(主 Agent 对话引擎)

[第 05 期] 工具循环:while True → LLM → 有 tool_calls? → 执行工具 → 回灌 → 继续
                                         └→ 无 tool_calls? → 返回回答
[第 12 期] 循环里挂上 Hooks 钩子链:每次工具调用先过 Before(拦截/改写),
           执行(或被拦截)后再过 After(审计落盘)。工具本身一行不用改。
"""
import json
import time

from .config import client, MODEL
from .memory import append_and_persist
from .tools import TOOLS, execute_tool
from .hooks import run_before_hooks, run_after_hooks


def chat_with_tools(messages: list[dict], session_id: str | None = None) -> tuple[str, list[dict], int, int]:
    """带工具调用的对话。

    事件溯源:每条 assistant(tool_calls) / tool / assistant(回答)
    在 append 到 messages 后立即 persist_one 落盘,产生即持久化。

    第 12 期新增:每次工具调用先过 Before 钩子链(拦截/改写),
    执行(或被拦截)后再过 After 钩子链(审计落盘)。
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
