"""main.py — 第 14 期主流程入口

累积式:step14 = step13 + 目标驱动(goal contract + 控制循环 + checkpoint)
保留 step01~13 全部能力(工具循环 + 记忆 + 技能 + TodoList + 子代理 +
Agent Team + MCP + Hooks + 多模型路由),新增目标驱动层(目标契约 +
run_goal_loop 控制循环 + 3 工具 + goal_log 落盘 + 目标面板)。

运行:
    cd code && python -m step14_goal_agent
"""
import os
from datetime import datetime

from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

from .config import (
    client, MODEL, MAX_ROUNDS, COMPACT_THRESHOLD_ROUNDS,
    SOUL_PATH, USER_PATH, TODO_GUIDE_PATH, RAW_HISTORY_PATH,
    AUDIT_LOG_PATH, USAGE_LOG_PATH, GOAL_LOG_PATH, AGENT_NAME,
)
from .prompt import load_system_prompt, load_user_profile, load_skills_index, build_skills_prompt, print_alex
from .memory import (
    append_and_persist, log_session, compact_history,
    read_recent_raw_history, print_memory_dashboard, trim_history,
)
from .todo import print_todo_panel, _todo_list
from .subagent import SUBAGENT_TYPES, _subagent_runs, print_subagent_panel
from .team import TEAM_MEMBERS, print_team_panel
from .mcp import MCP_SERVERS, print_mcp_panel, shutdown_mcp
from .hooks import print_hooks_panel, print_audit_panel, _audit_count
from .model_router import (
    MODEL_REGISTRY, ROUTER_STATE, TIER_DEFAULT, BACKGROUND_MODEL,
    print_model_panel, print_cost_panel,
)
from .tools import TOOLS
from .agent import chat_with_tools
from .goal import print_goal_panel, run_goal_loop
# 注意:GOAL_STATE 会被 set_goal 整体重新赋值,from .goal import GOAL_STATE
# 只会拿到导入时的快照(None),所以这里导入模块本身,用 goal.GOAL_STATE 读实时值
from . import goal


def main():
    print("=" * 60)
    print("第 14 期:目标驱动 Agent —— goal contract + 控制循环 + checkpoint")
    print("=" * 60)
    print(f"当前模型池: {', '.join(MODEL_REGISTRY.keys())}")
    print(f"路由模式: {ROUTER_STATE['mode']}(简单→{TIER_DEFAULT['light']},复杂→{TIER_DEFAULT['heavy']})")
    print(f"后台任务(compact/子代理/队友)固定: {BACKGROUND_MODEL}")
    print(f"API 地址: {client.base_url}(教学:全部走 DeepSeek 端点,按注册表模拟计费)")
    print(f"上下文上限: {MAX_ROUNDS} 轮(超出自动截断)")
    print(f"压缩阈值: {COMPACT_THRESHOLD_ROUNDS} 轮(超出自动 compact)")
    print(f"已装工具: {', '.join(t['function']['name'] for t in TOOLS)}")

    skills_index = load_skills_index()
    print(f"技能索引: {len(skills_index)} 个")
    print(f"子代理类型: {', '.join(SUBAGENT_TYPES.keys())}")
    print(f"团队成员: {', '.join(TEAM_MEMBERS.keys())}")

    # 加载三层记忆
    soul = load_system_prompt()
    user_profile = load_user_profile()
    skills_prompt = build_skills_prompt(skills_index)

    # 拼装 system prompt:人设 + 技能索引 + 用户画像 + TodoList 指南 + Subagent 指南 + Team 指南
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

    # Agent Team 工作指南(内联,注入 system prompt)
    team_guide = (
        "\n\n## Agent Team 使用指南\n"
        "你可以通过 team_wake 唤醒持久队友。队友的对话历史和 inbox 跨唤醒保持。\n"
        "队友之间可以通过 send_message 互发消息,通过 broadcast_message 广播消息。\n"
        "决策流程:\n"
        "1. 当任务需要多个角色多轮协作时(如前端+后端合作开发),用 team_wake 逐个唤醒\n"
        "2. 当需要给队友传话时,用 send_message 发到对方 inbox\n"
        "3. 当需要通知所有人时,用 broadcast_message 广播\n"
        "4. 当需要查看队友状态时,用 list_team\n"
        "5. 当需要查看队友收到的消息时,用 read_inbox\n"
        "关键差异:子agent 是临时的(用完即弃),队友是持久的(上下文跨唤醒保持)。\n"
        "使用模式:唤醒前端 → 前端给后端发消息要接口 → 唤醒后端 → 后端回复 → 再唤醒前端对接。\n"
    )
    system_prompt += team_guide
    print(f"[Team 指南] 内联注入({len(team_guide)} 字符)")

    # MCP 使用指南(内联,注入 system prompt)
    mcp_guide = (
        "\n\n## MCP 使用指南\n"
        "你可以通过 MCP(Model Context Protocol)连接外部工具服务器。"
        "MCP 服务器是独立进程,通过 stdio(JSON-RPC 2.0)与你通信,它的工具不写在你自己的代码里。\n"
        "决策流程:\n"
        "1. 先用 list_mcp_servers 查看有哪些可用的 MCP 服务器\n"
        "2. 用 mcp_connect 连接服务器(握手 + 拉取工具清单)\n"
        "3. 不确定工具名/参数时,用 mcp_list_tools 查看服务器提供哪些工具及其参数 schema\n"
        "4. 用 mcp_call_tool(server_id, tool_name, arguments) 调用服务器上的工具\n"
        "与内置工具的区别:内置工具(get_current_time、calculate)写死在你的代码里;"
        "MCP 工具在外部服务器进程里,连上就能用,服务器升级/更换你一行代码都不用改。\n"
        "典型场景:查 GitHub 仓库、查数据库、查文档——只要对方提供 MCP 服务器,你就能接。\n"
    )
    system_prompt += mcp_guide
    print(f"[MCP 指南] 内联注入({len(mcp_guide)} 字符)")

    # Hooks 使用指南(内联,注入 system prompt)
    hooks_guide = (
        "\n\n## Hooks 使用指南\n"
        "你的每一次工具调用都会经过 Hooks(钩子)检查:\n"
        "1. Before 钩子链(执行前):sensitive_word_guard 会拦截'落盘/外发'类工具"
        "(save_user_profile / mcp_call_tool / send_message 等)参数中的敏感词"
        "(密码、密钥、身份证等);sensitive_word_masker 会把其他工具参数中的敏感词打码。\n"
        "2. After 钩子链(执行后):audit_logger 把每次调用(含被拦截的)写入审计日志。\n"
        "决策流程:\n"
        "1. 用户想知道你的安全机制时,用 list_hooks 查看钩子链\n"
        "2. 用户想回放'你刚才做了什么'时,用 view_audit_log 查看审计日志\n"
        "3. 工具返回'被 Before Hook 拦截'的错误时,不要重试原参数,应向用户说明"
        "该信息属于敏感数据、无法保存或外发,并建议脱敏后再试\n"
        "重要:被拦截不是故障,是安全机制在工作。审计日志里的 blocked 状态就是证据。\n"
    )
    system_prompt += hooks_guide
    print(f"[Hooks 指南] 内联注入({len(hooks_guide)} 字符)")

    # 多模型路由指南(第 13 期内联,注入 system prompt)
    router_guide = (
        "\n\n## 多模型路由指南\n"
        "你可以使用多个模型,由路由器自动决定每次调用用哪个:\n"
        f"- light 档({TIER_DEFAULT['light']}):便宜、快,处理简单问答和后台任务\n"
        f"- heavy 档({TIER_DEFAULT['heavy']}):深度推理,处理复杂分析(命中'审查/分析/设计/对比'等关键词时自动升级)\n"
        f"- 其他模型(claude-sonnet/local-qwen)也已在注册表里,可手动钉死\n"
        "- 后台任务(compact 压缩、子代理、队友)固定走 light 档,这是省钱策略\n"
        "决策流程:\n"
        "1. 用户问'你有哪些模型/现在用哪个'时,用 list_models 查看注册表和路由状态\n"
        "2. 用户说'换便宜点的模型'/'接下来都用 claude'时,用 switch_model 切换"
        "(mode='auto' 恢复自动,mode='manual'+model_id 钉死)\n"
        "3. 用户问'花了多少钱'/'哪个模型最贵'时,用 cost_report 查看成本 telemetry\n"
        "重要:路由对你透明 —— 你的每次调用都已被记账,成本数据是真实的,可以放心引用 cost_report 的数字。\n"
    )
    system_prompt += router_guide
    print(f"[路由指南] 内联注入({len(router_guide)} 字符)")

    # 目标驱动指南(第 14 期内联,注入 system prompt)
    goal_guide = (
        "\n\n## 目标驱动使用指南\n"
        "你可以围绕'目标契约'自主完成多轮任务:\n"
        "1. 用户提出模糊目标(如'帮我优化这个仓库的 README')时,建议用户用 '目标:' 前缀启动控制循环自主推进;\n"
        "   用户没用前缀就按普通对话处理,不要自行 set_goal(避免订约后没人推进的悬空契约)\n"
        "   前缀触发后控制循环会指示你调 set_goal 订契约:\n"
        "   goal(一句话说清做什么)+ success_criteria(2-4 条可判定的验收标准)+ max_rounds(预算)\n"
        "2. 每轮工作结束必须用 check_goal 对照验收标准如实自评:\n"
        "   全部达标→done;缺权限/缺信息做不下去→blocked;确信做不成→failed;还没完→in_progress\n"
        "3. 用户问'目标做到哪了'时,用 goal_status 查看契约和 checkpoint 历史\n"
        "重要:\n"
        "- 验收标准必须可逐条判定(写'包含逐节修改建议',不写'更好')\n"
        "- 自评必须如实,禁止未达标就报 done —— 控制循环按你的自评决定继续或停止\n"
        "- 控制循环有预算硬上限:预算耗尽仍未达标会被标记 failed,这是止损机制不是惩罚\n"
        "- 与 TodoList 配合:TodoList 管'这一轮先做什么后做什么',目标契约管'几轮之后什么时候算完'"
    )
    system_prompt += goal_guide
    print(f"[目标驱动指南] 内联注入({len(goal_guide)} 字符)")

    # ============ 记忆面板 ============
    print_memory_dashboard()

    # TodoList 面板(初始为空)
    print_todo_panel()

    # Subagent 面板(初始为空)
    print_subagent_panel()

    # Team 面板(初始为空)
    print_team_panel()

    # MCP 面板(初始为未连接)
    print_mcp_panel()

    # Hooks 面板(钩子链 + 敏感词表)
    print_hooks_panel()

    # 审计面板(可能为空,也可能有历史记录)
    print_audit_panel()

    # 模型路由面板(第 13 期新增:模型池 + 路由模式 + 注册表)
    print_model_panel()

    # 成本面板(第 13 期新增:历史账单可能为空)
    print_cost_panel()

    # 目标面板(第 14 期新增:无活跃目标时显示占位)
    print_goal_panel()

    # 演示 session_id
    demo_session_id = f"demo-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # ============ 自动演示 1:模糊目标全流程(订约 → 执行 → checkpoint → 判停) ============
    print('\n[自动演示 1] 模糊目标全流程:"帮我优化这个仓库的 README" → 控制循环自主推进:\n')

    try:
        run_goal_loop(
            "帮我优化这个仓库的 README",
            demo_session_id,
            system_prompt,
            budget_rounds=2,   # 演示收紧预算:订约轮 + 执行轮,封顶 2 轮
        )
    except Exception as e:
        print(f"[出错] {e}")

    print("\n" + "─" * 60)

    # ============ 自动演示 2:目标复盘(goal_status + cost_report) ============
    print('\n[自动演示 2] 目标复盘:checkpoint 历史 + 这次自主任务花了多少钱:\n')
    demo_messages2 = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "刚才那个目标做完了,帮我做两件事:\n"
            "1. 用 goal_status 把目标的 checkpoint 历史汇报给我:"
            "每轮自评是什么状态、依据是什么\n"
            "2. 用 cost_report 看看这次自主任务一共花了多少钱,"
            "把数字汇报给我"
        )},
    ]
    try:
        answer, demo_messages2, p, c = chat_with_tools(demo_messages2, demo_session_id)
        log_session(demo_session_id, 1, p, c)
        print_alex(f"\n{AGENT_NAME}: {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
        print_goal_panel()
        print_cost_panel()
    except Exception as e:
        print(f"[出错] {e}")

    print("\n" + "─" * 60)

    # ============ 自由对话(累积式) ============
    print("\n现在进入自由对话(Alex + 人设 + 技能库 + 长期记忆 + TodoList + 子代理 + Agent Team + MCP + Hooks + 多模型路由 + 目标驱动)")
    print("输入 quit 退出 / reset 清空 / history 历史 / memory 记忆面板")
    print("     soul 人设 / skills 技能索引 / tools 工具列表 / todo 任务面板")
    print("     subagents 子代理面板 / dispatch 手动派遣")
    print("     team 团队面板 / wake 手动唤醒队友")
    print("     mcp MCP 面板(外部工具服务器)")
    print("     hooks 钩子链面板 / audit 审计日志面板")
    print("     models 模型路由面板 / cost 成本面板(telemetry)")
    print("     goal 目标面板(契约 + checkpoint)")
    print("     目标:xxx 进入目标驱动模式(控制循环自主推进到达标/预算耗尽)")
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

    print_team_panel()

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
            shutdown_mcp()  # 关掉所有 MCP 子进程,不留孤儿进程
            print(f"\n[本次会话] 轮数={rounds}  prompt={total_prompt}  "
                  f"completion={total_completion}  total={total_prompt + total_completion}")
            print(f"[raw_history] {RAW_HISTORY_PATH}(每轮对话已实时落盘)")
            print(f"[audit_log] {AUDIT_LOG_PATH}(每次工具调用/拦截已实时审计)")
            print(f"[usage_log] {USAGE_LOG_PATH}(每次 LLM 调用成本已实时记账)")
            print(f"[goal_log] {GOAL_LOG_PATH}(每轮 checkpoint 自评已实时落盘)")
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
            print(f"[人设 + 技能 + 用户画像 + TodoList 指南 + Subagent 指南 + Team 指南 + MCP 指南 + Hooks 指南 + 路由指南]:\n{system_prompt[:800]}...\n")
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
        if user_input.lower() == "team":
            print_team_panel()
            continue
        if user_input.lower() == "mcp":
            print_mcp_panel()
            print("\n[MCP 服务器]")
            for sid, cfg in MCP_SERVERS.items():
                connected = "已连接" if cfg["conn"] is not None else "未连接"
                print(f"  - {sid}: {cfg['description']}")
                print(f"    状态={connected}  工具数={len(cfg['tools'])}")
            print("\n[提示] 问 Alex:'帮我通过 MCP 查一下 deepseek-ai 的仓库信息',它会自己连服务器、调工具。")
            continue
        if user_input.lower() == "hooks":
            print_hooks_panel()
            print("\n[提示] 问 Alex:'你有哪些安全钩子?',它会调用 list_hooks 汇报钩子链。")
            continue
        if user_input.lower() == "audit":
            print_audit_panel()
            print("\n[提示] 问 Alex:'把审计日志最近 10 条读给我听',它会调用 view_audit_log。")
            continue
        if user_input.lower() == "models":
            print_model_panel()
            print("\n[提示] 问 Alex:'你有哪些模型?现在用的是哪个?',它会调用 list_models。")
            print("[提示] 说'接下来都用 claude-sonnet 回答',它会调用 switch_model 钉死模型。")
            continue
        if user_input.lower() == "cost":
            print_cost_panel()
            print("\n[提示] 问 Alex:'这次对话花了多少钱?',它会调用 cost_report 汇报账单。")
            continue
        if user_input.lower() == "goal":
            print_goal_panel()
            print("\n[提示] 输入'目标:优化这个仓库的 README'进入控制循环,Alex 会自主推进到达标或预算耗尽。")
            print("[提示] 问 Alex:'目标做到哪了?',它会调用 goal_status 汇报进度。")
            continue
        if user_input.lower() == "wake":
            print("[团队成员]")
            for mid, config in TEAM_MEMBERS.items():
                inbox_n = len(config["inbox"])
                wakes = config["wake_count"]
                has_ctx = "有" if config["messages"] is not None else "无"
                print(f"  - {mid}: {config['description']}")
                print(f"    inbox={inbox_n} 未读  唤醒={wakes} 次  上下文={has_ctx}")
                print(f"    允许工具: {', '.join(config['allowed_tools'])}")
            print("\n[提示] 问 Alex 一个问题,如'让前端和后端协作开发登录功能',它会自己组队。")
            continue
        if not user_input:
            continue

        # 第 14 期:目标分流器 —— 输入以"目标:"/"goal:"开头 → 进入控制循环自主推进
        # (全角冒号"："归一化为半角,中文输入法下也能命中)
        _goal_input = user_input.lstrip().replace("：", ":")
        if _goal_input.startswith("目标:") or _goal_input.lower().startswith("goal:"):
            fuzzy_goal = _goal_input.split(":", 1)[1].strip()
            if fuzzy_goal:
                try:
                    run_goal_loop(fuzzy_goal, session_id, system_prompt)
                except Exception as e:
                    print(f"[出错] {e}")
                continue
            print("[提示] 目标不能为空,如:目标:优化这个仓库的 README")
            continue

        append_and_persist(messages, {"role": "user", "content": user_input}, session_id)
        rounds += 1

        messages = trim_history(messages)
        if rounds > COMPACT_THRESHOLD_ROUNDS and sum(1 for m in messages if m["role"] == "user") > COMPACT_THRESHOLD_ROUNDS:
            print("[compact] 触发自动压缩...")
            messages = compact_history(messages)

        audit_before = _audit_count()
        try:
            answer, messages, p, c = chat_with_tools(messages, session_id)
            total_prompt += p
            total_completion += c
            print_alex(f"{AGENT_NAME}: {answer}")
            print(f"[token] 本次 prompt={p}  completion={c}  "
                  f"累计 total={total_prompt + total_completion}  轮数={rounds}")
            # 每轮对话后如果有子代理活动,打印面板
            if _subagent_runs:
                print_subagent_panel()
            if _todo_list:
                print_todo_panel()
            # 如果团队有活动,打印团队面板
            team_active = any(
                cfg["wake_count"] > 0 or cfg["inbox"]
                for cfg in TEAM_MEMBERS.values()
            )
            if team_active:
                print_team_panel()
            # 如果有 MCP 服务器连接活动,打印 MCP 面板
            mcp_active = any(cfg["conn"] is not None for cfg in MCP_SERVERS.values())
            if mcp_active:
                print_mcp_panel()
            # 如果本轮有新的审计记录(工具被调用/拦截),打印审计面板
            if _audit_count() > audit_before:
                print_audit_panel(limit=5)
            # 第 13 期:每次 LLM 调用都有路由决策,打印模型路由面板(含当次模型)
            if ROUTER_STATE["history"]:
                print_model_panel()
            # 第 14 期:本轮有目标活动(set_goal/check_goal),打印目标面板
            if goal.GOAL_STATE is not None:
                print_goal_panel()
        except Exception as e:
            messages.pop()
            rounds -= 1
            print(f"[出错] {e}")
            continue

        log_session(session_id, rounds, total_prompt, total_completion)


if __name__ == "__main__":
    main()
