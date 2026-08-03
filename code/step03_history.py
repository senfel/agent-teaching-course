#!/usr/bin/env python3
"""step03_history.py — 第 03 期:短期记忆 history

本期目标:
1. 维护一个 messages 列表,每次调用把完整对话历史回灌进去
2. 让模型真正"记住"上一句,解决第 02 期的"金鱼记忆"
3. 引入上下文长度限制,历史不能无限长——超限时做最简单的截断

累积式:step03 = step02 + messages[] 回灌 + 上下文长度限制
(尚未做记忆压缩/长期记忆,后续第 07 期解决)

运行:
    python code/step03_history.py
"""
import os
from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

load_dotenv()

# ============ 1. 初始化客户端(与第 01/02 期一致)============
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# 上下文长度上限(轮数,1 轮 = 1 user + 1 assistant)
# 超过此值时,从最早的一轮开始截断(保留最近 MAX_ROUNDS 轮)
MAX_ROUNDS = 10


def chat(messages: list[dict]) -> tuple[str, int, int]:
    """带历史回灌的对话:传入完整 messages 列表,返回 (回答, prompt_tokens, completion_tokens)。

    与第 02 期的关键差异:
    - 第 02 期:messages 只含当前这一句,模型看不到上一句
    - 第 03 期:messages 是完整对话历史,模型能"记住"之前说过什么
    """
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=1000,
    )
    answer = response.choices[0].message.content
    usage = response.usage
    return answer, usage.prompt_tokens, usage.completion_tokens


def trim_history(messages: list[dict]) -> list[dict]:
    """上下文长度限制:超过 MAX_ROUNDS 轮时,从最早的一轮开始截断。

    简单截断策略(第 07 期会升级为 compact 压缩):
    - 1 轮 = 1 条 user + 1 条 assistant = 2 条
    - 超过 MAX_ROUNDS 轮,删除最早的 (超出轮数 × 2) 条
    - 始终保留最近 MAX_ROUNDS 轮
    """
    rounds = sum(1 for m in messages if m["role"] == "user")
    if rounds <= MAX_ROUNDS:
        return messages
    # 计算需要截断的轮数,每轮 2 条消息
    excess_rounds = rounds - MAX_ROUNDS
    cut = excess_rounds * 2
    return messages[cut:]


def main():
    print("=" * 60)
    print("第 03 期:短期记忆 history —— 让模型记住上一句")
    print("=" * 60)
    print(f"当前模型: {MODEL}")
    print(f"API 地址: {client.base_url}")
    print(f"上下文上限: {MAX_ROUNDS} 轮(超出自动截断最早)")
    print("=" * 60)

    # ============ 2. 自动演示:对比第 02 期的"金鱼记忆" ============
    # 同样两个问题,这次模型应该能答上来
    demo_questions = [
        "我叫张三,是一名 Python 后端工程师",
        "我姓什么?我是做什么工作的?",
    ]
    print("\n[自动演示] 同样两个问题,这次模型能否记住上一句:\n")

    # 用一个共享的 messages 列表维护历史
    messages: list[dict] = []
    for i, q in enumerate(demo_questions, 1):
        print(f"[问题 {i}] {q}")
        messages.append({"role": "user", "content": q})
        try:
            answer, p, c = chat(messages)
            # 把回答也加回历史,供下一轮使用
            messages.append({"role": "assistant", "content": answer})
            print(f"[回答] {answer}")
            print(f"[token] prompt={p}  completion={c}  total={p + c}")
            print(f"[历史] 当前 messages 共 {len(messages)} 条")
        except Exception as e:
            print(f"[出错] {e}")
            return
        print("-" * 60)

    print(
        "\n💡 对比第 02 期:这次模型答上来了——"
        "因为 messages 里带着上一轮的问答,模型「看到」了完整上下文。\n"
        "   但注意 prompt_tokens 比第 02 期大,因为历史一起发了过去。\n"
    )

    # ============ 3. 交互式对话(带历史回灌)============
    print("=" * 60)
    print("现在进入自由对话(有记忆,但超过 10 轮自动截断最早)")
    print("输入 quit 退出  /  输入 reset 清空历史  /  输入 history 查看历史")
    print("←/→ 移动光标,↑/↓ 翻历史,Ctrl-C 作废当前行重输")
    print("=" * 60)

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
            print(f"[历史] 退出时共 {len(messages)} 条消息")
            print("再见!")
            break
        if user_input.lower() == "reset":
            messages.clear()
            total_prompt = 0
            total_completion = 0
            print("[提示] 历史已清空,重新开始对话。")
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

        # 把用户输入加入历史
        messages.append({"role": "user", "content": user_input})
        # 上下文长度限制:超限时截断最早的一轮
        messages = trim_history(messages)
        try:
            answer, p, c = chat(messages)
            total_prompt += p
            total_completion += c
            # 把模型回答也加回历史
            messages.append({"role": "assistant", "content": answer})
            print(f"Alex: {answer}")
            print(f"[token] 本次 prompt={p}  completion={c}  "
                  f"累计 total={total_prompt + total_completion}")
            print(f"[历史] 当前 {len(messages)} 条消息"
                  f"({sum(1 for m in messages if m['role'] == 'user')} 轮)")
        except Exception as e:
            # 调用失败时,把刚加入的用户消息撤回,避免历史污染
            messages.pop()
            print(f"[出错] {e}")


if __name__ == "__main__":
    main()
