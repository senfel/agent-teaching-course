"""main.py — 第 09 期主流程入口

累积式:step09 = step08 + Subagent
保留 step01~08 全部能力(工具循环 + 记忆 + 技能 + TodoList),
新增 Subagent 层(代码审查员 + 文档整理员 并发派遣)。

运行:
    cd code && python -m step09_subagent
"""
import os
from datetime import datetime

from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

from .config import (
    client, MODEL, MAX_ROUNDS, COMPACT_THRESHOLD_ROUNDS,
    SOUL_PATH, USER_PATH, TODO_GUIDE_PATH, RAW_HISTORY_PATH,
)
from .prompt import load_system_prompt, load_user_profile, load_skills_index, build_skills_prompt
from .memory import (
    append_and_persist, log_session, compact_history,
    read_recent_raw_history, print_memory_dashboard, trim_history,
)
from .todo import print_todo_panel, _todo_list
from .subagent import SUBAGENT_TYPES, _subagent_runs, print_subagent_panel
from .tools import TOOLS
from .agent import chat_with_tools


def main():
    print("=" * 60)
    print("第 09 期:子代理 Subagent —— 让 Alex 学会派活给别人")
    print("=" * 60)
    print(f"当前模型: {MODEL}")
    print(f"API 地址: {client.base_url}")
    print(f"上下文上限: {MAX_ROUNDS} 轮(超出自动截断)")
    print(f"压缩阈值: {COMPACT_THRESHOLD_ROUNDS} 轮(超出自动 compact)")
    print(f"已装工具: {', '.join(t['function']['name'] for t in TOOLS)}")

    skills_index = load_skills_index()
    print(f"技能索引: {len(skills_index)} 个")
    print(f"子代理类型: {', '.join(SUBAGENT_TYPES.keys())}")

    # 加载三层记忆
    soul = load_system_prompt()
    user_profile = load_user_profile()
    skills_prompt = build_skills_prompt(skills_index)

    # 拼装 system prompt:人设 + 技能索引 + 用户画像 + TodoList 指南 + Subagent 指南
    system_prompt = soul
    if skills_prompt:
        system_prompt += "\n" + skills_prompt
    if user_profile:
        system_prompt += "\n\n# 用户画像(长期记忆)\n\n" + user_profile
        print(f"[人设] {SOUL_PATH}")
        print(f"[用户画像] {USER_PATH}({len(user_profile)} 字符)")
    else:
        print("[用户画像] 未加载")

    # TodoList 工作指南(从模板文件加载,注入 system prompt)
    if TODO_GUIDE_PATH.exists():
        todo_guide = "\n" + TODO_GUIDE_PATH.read_text(encoding="utf-8")
        system_prompt += todo_guide
        print(f"[TodoList 指南] {TODO_GUIDE_PATH}({len(todo_guide)} 字符)")
    else:
        print(f"[TodoList 指南] 未加载:{TODO_GUIDE_PATH} 不存在")

    # Subagent 工作指南(内联,注入 system prompt)
    subagent_guide = (
        "\n\n## 子代理使用指南\n"
        "你可以通过 dispatch_subagent 派遣单个子代理,或通过 dispatch_subagents_parallel 并发派遣多个。\n"
        "决策流程:\n"
        "1. 当任务涉及多个专家角色(如代码审查 + 文档整理)时,用 dispatch_subagents_parallel 并发派遣\n"
        "2. 当任务只需要单个专家时,用 dispatch_subagent 派遣一个\n"
        "3. 不确定有哪些子代理时,用 list_subagents 查看\n"
        "重要:子代理有独立上下文和工具白名单,它们的对话不会出现在你的上下文里。\n"
        "你收到的是子代理的最终结果(文本),不是它们的对话过程。\n"
    )
    system_prompt += subagent_guide
    print(f"[Subagent 指南] 内联注入({len(subagent_guide)} 字符)")

    # ============ 记忆面板 ============
    print_memory_dashboard()

    # TodoList 面板(初始为空)
    print_todo_panel()

    # Subagent 面板(初始为空)
    print_subagent_panel()

    # 演示 session_id
    demo_session_id = f"demo-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # ============ 自动演示:并发派遣"代码审查员"+"文档整理员" ============
    demo_code = '''def fetch_user(uid):
    users = {1: "Alice", 2: "Bob"}
    return users[uid]

def calculate_price(qty, unit_price):
    total = qty * unit_price
    discount = 0.1
    return total - total * discount'''

    print('[自动演示] 并发派遣"代码审查员" + "文档整理员":\n')
    print(f'  代码:\n{demo_code}\n')

    demo_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"请帮我做两件事:\n"
            f"1. 审查这段代码,指出问题和改进建议\n"
            f"2. 把这段代码整理成一份技术文档(含函数说明和用法示例)\n\n"
            f"代码:\n```python\n{demo_code}\n```"
        )},
    ]
    try:
        answer, demo_messages, p, c = chat_with_tools(demo_messages, demo_session_id)
        log_session(demo_session_id, 1, p, c)
        print(f"\n[Alex] {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
        print_subagent_panel()
    except Exception as e:
        print(f"[出错] {e}")

    print("\n" + "─" * 60)

    # ============ 自动演示 2:单独派遣一个子代理 ============
    print('\n[自动演示 2] 单独派遣"文档整理员":\n')
    demo_messages2 = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "帮我把这句话整理成规范的技术文档格式:'这个函数用来算价格,输入数量和单价,返回打折后的总价'"},
    ]
    try:
        answer, demo_messages2, p, c = chat_with_tools(demo_messages2, demo_session_id)
        log_session(demo_session_id, 1, p, c)
        print(f"\n[Alex] {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
        print_subagent_panel()
    except Exception as e:
        print(f"[出错] {e}")

    print("\n" + "─" * 60)

    # ============ 自由对话(累积式) ============
    print("\n现在进入自由对话(Alex + 人设 + 技能库 + 长期记忆 + TodoList + 子代理)")
    print("输入 quit 退出 / reset 清空 / history 历史 / memory 记忆面板")
    print("     soul 人设 / skills 技能索引 / tools 工具列表 / todo 任务面板")
    print("     subagents 子代理面板 / dispatch 手动派遣")
    print("←/→ 光标,↑/↓ 历史,Ctrl-C 作废本行")
    print("=" * 60)

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

    print_subagent_panel()

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    total_prompt = 0
    total_completion = 0
    cli_history = InMemoryHistory()
    rounds = 0

    while True:
        try:
            user_input = prompt("\n你: ", history=cli_history).strip()
        except (EOFError, KeyboardInterrupt):
            print("  (本行作废)")
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            log_session(session_id, rounds, total_prompt, total_completion)
            print(f"\n[本次会话] 轮数={rounds}  prompt={total_prompt}  "
                  f"completion={total_completion}  total={total_prompt + total_completion}")
            print(f"[raw_history] {RAW_HISTORY_PATH}(每轮对话已实时落盘)")
            print("再见!")
            break
        if user_input.lower() == "reset":
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
            print(f"[人设 + 技能 + 用户画像 + TodoList 指南 + Subagent 指南]:\n{system_prompt[:800]}...\n")
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
        if user_input.lower() == "todo":
            print_todo_panel()
            continue
        if user_input.lower() == "subagents":
            print_subagent_panel()
            continue
        if user_input.lower() == "dispatch":
            print("[子代理类型]")
            for name, config in SUBAGENT_TYPES.items():
                print(f"  - {name}: {config['description']}")
                print(f"    允许工具: {', '.join(config['allowed_tools'])}")
            print("\n[提示] 问 Alex 一个问题,如'帮我审查这段代码并整理文档',它会自己派子代理。")
            continue
        if not user_input:
            continue

        append_and_persist(messages, {"role": "user", "content": user_input}, session_id)
        rounds += 1

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
            # 每轮对话后如果有子代理活动,打印面板
            if _subagent_runs:
                print_subagent_panel()
            if _todo_list:
                print_todo_panel()
        except Exception as e:
            messages.pop()
            rounds -= 1
            print(f"[出错] {e}")
            continue

        log_session(session_id, rounds, total_prompt, total_completion)


if __name__ == "__main__":
    main()
