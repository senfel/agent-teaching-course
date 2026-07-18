#!/usr/bin/env python3
"""step01_hello_agent.py — 第 01 期:认识 Agent + 开工准备

本期目标:
1. 配置好 DeepSeek API Key
2. 能跑通第一次模型调用
3. 理解 LLM 和 Agent 的区别(为后续铺垫)

运行:
    python code/step01_hello_agent.py
"""
import os
from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

load_dotenv()

# ============ 1. 初始化客户端 ============
# DeepSeek 兼容 OpenAI 协议,所以直接用 openai SDK
# 关键配置:
#   - api_key:你在 platform.deepseek.com 创建的 Key
#   - base_url:固定为 https://api.deepseek.com
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def chat(user_input: str) -> str:
    """单次对话:输入一句话,返回模型回答。"""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": user_input}],
        max_tokens=1000,
    )
    return response.choices[0].message.content


def main():
    print("=" * 60)
    print("第 01 期:Hello Agent — 你的第一次模型调用")
    print("=" * 60)
    print(f"当前模型: {MODEL}")
    print(f"API 地址: {client.base_url}")
    print("=" * 60)

    # 试试三个问题,感受 LLM 的能力边界
    questions = [
        "你好,请用一句话介绍你自己",
        "用 Python 写一个冒泡排序,只要代码不要解释",
        "帮我算一下 17 * 23 等于多少",
    ]

    for i, q in enumerate(questions, 1):
        print(f"\n[问题 {i}] {q}")
        try:
            answer = chat(q)
            print(f"[回答] {answer}")
        except Exception as e:
            print(f"[出错] {e}")
            print("→ 请检查 .env 里的 DEEPSEEK_API_KEY 是否正确")
            return

    # 交互式对话
    print("\n" + "=" * 60)
    print("现在可以自由对话,输入 quit 退出")
    print("←/→ 移动光标,↑/↓ 翻历史,Ctrl-C 作废当前行重输")
    print("=" * 60)

    history = InMemoryHistory()
    while True:
        try:
            user_input = prompt("\n你: ", history=history).strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl-D / Ctrl-C:作废当前行,继续下一轮(不退出程序)
            print("  (本行作废,重新输入)")
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("再见!")
            break
        if not user_input:
            continue
        try:
            print(f"Alex: {chat(user_input)}")
        except Exception as e:
            print(f"[出错] {e}")


if __name__ == "__main__":
    main()
