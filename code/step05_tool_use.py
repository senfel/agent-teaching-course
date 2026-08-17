#!/usr/bin/env python3
"""step05_tool_use.py — 第 05 期:Tool Use 工具调用

本期目标:
1. 给 Alex 装第一个工具 get_current_time,解决第 01 期"不知道几点"的伏笔
2. 实现"工具循环":模型决定调用工具 → 执行 → 把结果喂回去 → 生成最终回答
3. 理解 JSON schema 描述工具、tool 角色消息、tool_choice 参数

累积式:step05 = step04 + 工具定义 + 工具循环
(保留 step04 的 system prompt 人设 + history 记忆,只新增工具层)

运行:
    python code/step05_tool_use.py
"""
import os
import json
import ast
import operator
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

load_dotenv()

# ============ 1. 初始化客户端(与第 01-04 期一致)============
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# 上下文长度上限(轮数,1 轮 = 1 user + 1 assistant)
MAX_ROUNDS = 10

# ============ 2. 加载 System Prompt(与第 04 期一致)============
SOUL_PATH = Path(__file__).parent.parent / "templates" / "SOUL.md"


def load_system_prompt() -> str:
    """读取 templates/SOUL.md 作为 system prompt(第 04 期引入)。"""
    if not SOUL_PATH.exists():
        raise FileNotFoundError(
            f"找不到人设文件: {SOUL_PATH}\n"
            f"请确认 templates/SOUL.md 已创建(第 04 期引入)"
        )
    return SOUL_PATH.read_text(encoding="utf-8")


# ============ 3. 工具定义(本期新增)============
# 用 JSON schema 描述工具,模型据此决定何时调用、传什么参数
# DeepSeek 兼容 OpenAI function calling 协议

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。当用户问'现在几点''今天日期''今天星期几'等问题时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "目标时区,如 Asia/Shanghai、US/Eastern。默认 Asia/Shanghai。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "进行四则运算。当用户要求计算数学表达式时调用,支持加减乘除和括号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式,如 '17 * 23'、'100 + 200'、'(5 + 3) * 2'",
                    }
                },
                "required": ["expression"],
            },
        },
    },
]


# ============ 4. 工具实现(本期新增)============

# 安全的数学运算:用 AST 解析表达式,只允许四则运算节点
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_eval(node):
    """递归计算 AST 节点,只允许数字和四则运算。"""
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
    """执行工具,返回结果字符串。

    这是工具循环的"手"——模型决定调用哪个工具、传什么参数,
    实际执行在这里。真实场景里这里可能是 HTTP 请求、数据库查询等。
    """
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

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


# ============ 5. 工具循环(本期核心)============

def chat_with_tools(messages: list[dict]) -> tuple[str, list[dict], int, int]:
    """带工具调用的对话:返回 (最终回答, 更新后的messages, prompt_tokens, completion_tokens)。

    与第 04 期的差异:
    - 第 04 期:chat() 只调一次 API,直接拿文本回答
    - 第 05 期:可能需要多次调用——模型先说"我要调工具",
      执行工具后把结果喂回去,再调一次让模型生成最终回答

    工具循环流程:
      1. 用户问"现在几点?"
      2. 模型返回 tool_calls(而不是文本回答)
      3. 我们执行工具,拿到结果
      4. 把 tool 结果作为 role=tool 消息加入 messages
      5. 再次调用 API,模型拿到工具结果后生成最终回答
    """
    total_prompt = 0
    total_completion = 0

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",  # auto = 模型自己决定要不要调工具
            max_tokens=1000,
        )
        msg = response.choices[0].message
        total_prompt += response.usage.prompt_tokens
        total_completion += response.usage.completion_tokens

        # 情况 A:模型决定调用工具
        if msg.tool_calls:
            # 把 assistant 消息(含 tool_calls)加入历史
            # 注意:content 可能为 None(模型只返回了 tool_calls,没有文本)
            messages.append({
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
            # 逐个执行工具,把结果喂回去
            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)
                print(f"  [工具调用] {fn_name}({fn_args})")
                result = execute_tool(fn_name, fn_args)
                print(f"  [工具结果] {result}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
            # 继续循环,让模型拿到工具结果后生成最终回答
            continue

        # 情况 B:模型返回文本回答(没有调用工具)
        # 把最终回答也加入历史,这样 history 能看到完整的对话
        messages.append({"role": "assistant", "content": msg.content})
        return msg.content, messages, total_prompt, total_completion


def trim_history(messages: list[dict]) -> list[dict]:
    """上下文长度限制:超过 MAX_ROUNDS 轮时截断(与第 04 期一致)。

    system prompt 永远保留,只截断 user/assistant/tool 对话。
    """
    has_system = messages and messages[0]["role"] == "system"
    system_msg = [messages[0]] if has_system else []
    convo = messages[1:] if has_system else messages[:]

    rounds = sum(1 for m in convo if m["role"] == "user")
    if rounds <= MAX_ROUNDS:
        return messages
    excess_rounds = rounds - MAX_ROUNDS
    cut = excess_rounds * 2
    return system_msg + convo[cut:]


def main():
    print("=" * 60)
    print("第 05 期:Tool Use 工具调用 —— 给 Alex 装上\"手\"")
    print("=" * 60)
    print(f"当前模型: {MODEL}")
    print(f"API 地址: {client.base_url}")
    print(f"上下文上限: {MAX_ROUNDS} 轮(超出自动截断最早)")
    print(f"已装工具: {', '.join(t['function']['name'] for t in TOOLS)}")
    print("=" * 60)

    # ============ 6. 加载人设(与第 04 期一致)============
    try:
        system_prompt = load_system_prompt()
        print(f"[人设] 已加载: {SOUL_PATH}")
    except FileNotFoundError as e:
        print(f"[出错] {e}")
        return

    # ============ 7. 自动演示:工具调用全过程 ============
    print("\n[自动演示] 问\"现在几点了\",看工具循环全过程:\n")

    demo_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "现在几点了?今天星期几?"},
    ]
    try:
        answer, demo_messages, p, c = chat_with_tools(demo_messages)
        print(f"\n[最终回答] {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
        print(f"[历史] 工具循环结束后共 {len(demo_messages)} 条消息")
    except Exception as e:
        print(f"[出错] {e}")
        return

    print(
        "\n💡 看到了吗?模型没有\"猜\"时间,而是调用了 get_current_time 工具。\n"
        "   这是 Agent 和普通 LLM 的核心区别:Agent 能\"动手\",不只是\"动嘴\"。\n"
    )

    # ============ 8. 交互式对话(带人设 + 记忆 + 工具)============
    print("=" * 60)
    print("现在进入自由对话(Alex 技术助理,带记忆 + 人设 + 工具)")
    print("输入 quit 退出  /  reset 清空历史  /  history 查看历史  /  soul 查看人设  /  tools 查看工具")
    print("←/→ 移动光标,↑/↓ 翻历史,Ctrl-C 作废当前行重输")
    print("=" * 60)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    total_prompt = 0
    total_completion = 0
    cli_history = InMemoryHistory()

    while True:
        try:
            user_input = prompt("\n你: ", history=cli_history).strip()
        except (EOFError, KeyboardInterrupt):
            print("  (本行作废,重新输入)")
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print(f"\n[本次会话 token 汇总] prompt={total_prompt}  "
                  f"completion={total_completion}  total={total_prompt + total_completion}")
            print("再见!")
            break
        if user_input.lower() == "reset":
            messages = [{"role": "system", "content": system_prompt}]
            total_prompt = 0
            total_completion = 0
            print("[提示] 对话已清空,人设保留,Alex 还在。")
            continue
        if user_input.lower() == "history":
            print(f"[历史] 共 {len(messages)} 条消息:")
            for idx, m in enumerate(messages):
                role = m["role"]
                content = m.get("content", "")
                # tool_calls 消息可能 content 为空,展示工具调用名
                if m.get("tool_calls") and not content:
                    calls = ", ".join(
                        tc["function"]["name"] for tc in m["tool_calls"]
                    )
                    content = f"[tool_calls: {calls}]"
                elif not content:
                    content = "(无文本)"
                content = content[:60] + ("..." if len(content) > 60 else "")
                print(f"  {idx:2d}. [{role:9s}] {content}")
            continue
        if user_input.lower() == "soul":
            print(f"[人设] {SOUL_PATH}:\n")
            print(system_prompt)
            continue
        if user_input.lower() == "tools":
            print(f"[工具] 共 {len(TOOLS)} 个:")
            for t in TOOLS:
                fn = t["function"]
                params = fn.get("parameters", {}).get("properties", {})
                param_str = ", ".join(f"{k}: {v['description']}" for k, v in params.items())
                print(f"  - {fn['name']}({param_str})")
                print(f"    {fn['description']}")
            continue
        if user_input.lower() == "history":
            print(f"[历史] 共 {len(messages)} 条消息:")
            for idx, m in enumerate(messages):
                role = m["role"]
                content = m["content"][:50] + ("..." if len(m["content"]) > 50 else "")
                print(f"  {idx:2d}. [{role:9s}] {content}")
            continue
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})
        messages = trim_history(messages)
        try:
            answer, messages, p, c = chat_with_tools(messages)
            total_prompt += p
            total_completion += c
            print(f"Alex: {answer}")
            print(f"[token] 本次 prompt={p}  completion={c}  "
                  f"累计 total={total_prompt + total_completion}")
            print(f"[历史] 当前 {len(messages)} 条消息"
                  f"(含 1 条 system + "
                  f"{sum(1 for m in messages if m['role'] == 'user')} 轮对话)")
        except Exception as e:
            messages.pop()
            print(f"[出错] {e}")


if __name__ == "__main__":
    main()
