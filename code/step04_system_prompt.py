#!/usr/bin/env python3
"""step04_system_prompt.py — 第 04 期:System Prompt 人设

本期目标:
1. 引入 templates/SOUL.md,给 Agent 设定"技术助理 Alex"的角色
2. system prompt 作为 messages 列表的第一条,全程影响每次调用
3. 对比有无 system prompt 时的行为差异,直观看到"人设"的约束力

累积式:step04 = step03 + system prompt 人设
(在 step03 的 history 回灌基础上,只新增 system prompt 这一层)

运行:
    python code/step04_system_prompt.py
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

load_dotenv()

# ============ 1. 初始化客户端(与第 01-03 期一致)============
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# 上下文长度上限(轮数,1 轮 = 1 user + 1 assistant)
MAX_ROUNDS = 10

# ============ 2. 加载 System Prompt(本期新增)============
SOUL_PATH = Path(__file__).parent.parent / "templates" / "SOUL.md"


def load_system_prompt() -> str:
    """读取 templates/SOUL.md 作为 system prompt。

    这是本期唯一的新增能力:把人设从外部文件加载进来,
    作为 messages 列表的第一条(role=system),全程影响每次调用。
    """
    if not SOUL_PATH.exists():
        raise FileNotFoundError(
            f"找不到人设文件: {SOUL_PATH}\n"
            f"请确认 templates/SOUL.md 已创建(第 04 期引入)"
        )
    return SOUL_PATH.read_text(encoding="utf-8")


def chat(messages: list[dict]) -> tuple[str, int, int]:
    """带历史回灌 + system prompt 的对话。

    与第 03 期的差异:
    - 第 03 期:messages 只有 user/assistant,模型是"无身份"的通用助手
    - 第 04 期:messages 第一条是 system(Alex 技术助理),模型有了人设约束
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

    注意:system prompt(第一条)永远保留,只截断 user/assistant 对话。
    """
    # system prompt 不参与截断
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
    print("第 04 期:System Prompt 人设 —— 给 Agent 一个身份")
    print("=" * 60)
    print(f"当前模型: {MODEL}")
    print(f"API 地址: {client.base_url}")
    print(f"上下文上限: {MAX_ROUNDS} 轮(超出自动截断最早)")
    print("=" * 60)

    # ============ 3. 加载人设 ============
    try:
        system_prompt = load_system_prompt()
        print(f"[人设] 已加载: {SOUL_PATH}")
        print(f"[人设] 内容长度: {len(system_prompt)} 字符")
        print("-" * 60)
        print(system_prompt)
        print("-" * 60)
    except FileNotFoundError as e:
        print(f"[出错] {e}")
        return

    # ============ 4. 自动演示:对比有无 system prompt ============
    # 同一个问题,分别用"无 system"和"有 system"调用,看回答风格差异
    demo_question = "你好,你是谁?能帮我做什么?"

    print("\n[自动演示] 同一个问题,对比有无 system prompt:\n")

    # 场景 A:无 system prompt(和第 03 期一样)
    print("[场景 A] 无 system prompt:")
    no_system_msgs = [{"role": "user", "content": demo_question}]
    try:
        answer_a, p_a, c_a = chat(no_system_msgs)
        print(f"  回答: {answer_a}")
        print(f"  token: prompt={p_a}  completion={c_a}")
    except Exception as e:
        print(f"  [出错] {e}")
        return
    print("-" * 60)

    # 场景 B:有 system prompt(本期新增)
    print("[场景 B] 有 system prompt(Alex 技术助理):")
    with_system_msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": demo_question},
    ]
    try:
        answer_b, p_b, c_b = chat(with_system_msgs)
        print(f"  回答: {answer_b}")
        print(f"  token: prompt={p_b}  completion={c_b}")
    except Exception as e:
        print(f"  [出错] {e}")
        return
    print("-" * 60)

    print(
        "\n💡 对比:有 system prompt 时,模型自称 Alex、风格务实、不啰嗦;\n"
        "   没有 system prompt 时,模型是通用的、客套的、废话多。\n"
        "   注意 prompt_tokens 多了——system prompt 本身也占 token。\n"
    )

    # ============ 5. 交互式对话(带人设 + 历史回灌)============
    print("=" * 60)
    print("现在进入自由对话(Alex 技术助理,带记忆 + 人设)")
    print("输入 quit 退出  /  输入 reset 清空历史(保留人设)  /  输入 soul 查看人设")
    print("←/→ 移动光标,↑/↓ 翻历史,Ctrl-C 作废当前行重输")
    print("=" * 60)

    # messages 第一条永远是 system prompt
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
            print(f"[历史] 退出时共 {len(messages)} 条消息"
                  f"(含 1 条 system)")
            print("再见!")
            break
        if user_input.lower() == "reset":
            # 重置只清空对话,保留 system prompt
            messages = [{"role": "system", "content": system_prompt}]
            total_prompt = 0
            total_completion = 0
            print("[提示] 对话已清空,人设保留,Alex 还在。")
            continue
        if user_input.lower() == "soul":
            print(f"[人设] {SOUL_PATH}:\n")
            print(system_prompt)
            continue
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})
        messages = trim_history(messages)
        try:
            answer, p, c = chat(messages)
            total_prompt += p
            total_completion += c
            messages.append({"role": "assistant", "content": answer})
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
