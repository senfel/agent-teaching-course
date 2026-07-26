#!/usr/bin/env python3
"""step02_loop_no_memory.py — 第 02 期:单次调用 → 连续对话

本期目标:
1. 把第 01 期的"一问一答"升级成"循环对话"
2. 直观感受"无记忆"痛点(金鱼记忆)
3. 引入 token 计数可视化,看到上下文为空

累积式:step02 = step01 + while 循环 + token 计数
(尚未加入 history,下一期第 03 期解决记忆问题)

运行:
    python code/step02_loop_no_memory.py
"""
import os
from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

load_dotenv()

# ============ 1. 初始化客户端(与第 01 期一致)============
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def chat(user_input: str) -> tuple[str, int, int]:
    """单次对话:返回 (回答文本, prompt_tokens, completion_tokens)。

    注意:这里每次调用都是"独立的",messages 只包含当前这一句,
    模型看不到之前任何对话——这就是本期要暴露的"金鱼记忆"问题。
    """
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": user_input}],
        max_tokens=1000,
    )
    answer = response.choices[0].message.content
    usage = response.usage
    return answer, usage.prompt_tokens, usage.completion_tokens


def main():
    print("=" * 60)
    print("第 02 期:连续对话 —— 但它只有金鱼记忆")
    print("=" * 60)
    print(f"当前模型: {MODEL}")
    print(f"API 地址: {client.base_url}")
    print("=" * 60)

    # ============ 2. 自动演示:暴露"金鱼记忆"痛点 ============
    # 连续问两个有上下文依赖的问题,看模型能否接住
    demo_questions = [
        "我叫张三,是一名 Python 后端工程师",
        "我姓什么?我是做什么工作的?",
    ]
    print("\n[自动演示] 连续问两个问题,看模型能否记住上一句:\n")
    for i, q in enumerate(demo_questions, 1):
        print(f"[问题 {i}] {q}")
        try:
            answer, p, c = chat(q)
            print(f"[回答] {answer}")
            print(f"[token] prompt={p}  completion={c}  total={p + c}")
        except Exception as e:
            print(f"[出错] {e}")
            return
        print("-" * 60)

    print(
        "\n💡 看到了吗?第二个问题模型答不上来——"
        "因为它每次调用都是独立的,看不到上一句。\n"
        "   这就是本期要暴露的「金鱼记忆」痛点,下一期第 03 期用 history 解决。\n"
    )

    # ============ 3. 交互式对话(循环)============
    print("=" * 60)
    print("现在进入自由对话(注意:它依然没有记忆)")
    print("输入 quit 退出  /  输入 reset 重置(本期其实没东西可重置)")
    print("←/→ 移动光标,↑/↓ 翻历史,Ctrl-C 作废当前行重输")
    print("=" * 60)

    total_prompt = 0
    total_completion = 0

    history = InMemoryHistory()
    while True:
        try:
            user_input = prompt("\n你: ", history=history).strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl-D / Ctrl-C:作废当前行,继续下一轮(不退出程序)
            print("  (本行作废,重新输入)")
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print(f"\n[本次会话 token 汇总] prompt={total_prompt}  "
                  f"completion={total_completion}  total={total_prompt + total_completion}")
            print("再见!")
            break
        if user_input.lower() == "reset":
            print("[提示] 本期尚未实现记忆,无需重置。第 03 期才会有 reset 功能。")
            continue
        if not user_input:
            continue
        try:
            answer, p, c = chat(user_input)
            total_prompt += p
            total_completion += c
            print(f"Alex: {answer}")
            print(f"[token] 本次 prompt={p}  completion={c}  "
                  f"累计 total={total_prompt + total_completion}")
        except Exception as e:
            print(f"[出错] {e}")


if __name__ == "__main__":
    main()
