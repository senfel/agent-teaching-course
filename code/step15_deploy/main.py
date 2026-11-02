"""main.py — 第 15 期主流程入口(CLI 版)

累积式:step15 = step14 + 部署上线(Web UI + 流式响应 + Docker)
保留 step01~14 全部能力(工具循环 + 记忆 + 技能 + TodoList + 子代理 +
Agent Team + MCP + Hooks + 多模型路由 + 目标驱动)。CLI 与 Web 共用
同一份 system prompt(见 prompt.build_system_prompt);Web 入口在
web.py,默认 `python -m step15_deploy` 即启动 Web,`--cli` 才走本文件。

运行:
    cd code && python -m step15_deploy --cli
"""
import os
from datetime import datetime

from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

from .config import (
    client, MODEL, MAX_ROUNDS, COMPACT_THRESHOLD_ROUNDS,
    RAW_HISTORY_PATH, AUDIT_LOG_PATH, USAGE_LOG_PATH, GOAL_LOG_PATH, AGENT_NAME,
)
from .prompt import load_skills_index, build_system_prompt, print_alex
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
    print("第 15 期:部署上线 + 课程总结 —— Web UI + 流式响应 + Docker")
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

    # 第 15 期:system prompt 唯一来源(人设 + 技能 + 画像 + TodoList + 子代理
    # + Team + MCP + Hooks + 多模型路由 + 目标驱动),CLI 与 Web 共用同一份,
    # 不再在 main() 里内联拼装 —— 改 prompt.build_system_prompt 即两端同时生效。
    system_prompt = build_system_prompt()

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
            print(f"[人设 + 技能 + 用户画像 + TodoList 指南 + Subagent 指南 + Team 指南 + MCP 指南 + Hooks 指南 + 路由指南 + 目标驱动指南]:\n{system_prompt[:800]}...\n")
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
