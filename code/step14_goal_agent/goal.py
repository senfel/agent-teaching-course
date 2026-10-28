"""goal.py — 目标驱动层(第 14 期核心新增)

回答一个问题:目标模糊("帮我优化这个 README")时,Agent 怎么
"自己知道做到哪了、什么时候该停"?

三件套:
  1. 目标契约(GOAL_STATE):把模糊目标改写成结构化契约 ——
     goal(做什么)+ success_criteria[](怎么算完成,可逐条判定)
     + max_rounds(预算轮数)+ status + checkpoints[](自评历史)
  2. 控制循环(run_goal_loop):外层循环,一轮一轮推着 Agent 朝目标走,
     每轮结束看契约状态:done 停(达标)/ blocked 停(止损)/ 预算耗尽停(封顶)
  3. checkpoint(check_goal):每轮结束 Agent 对照验收标准自评一次,
     落盘 goal_log.jsonl —— 过程可回放,失败可复盘

与第 08 期 TodoList 的区别:TodoList 管"这一轮内先做什么后做什么"(一次
对话内的步骤清单);目标契约管"跨轮次的完成判定"(几轮对话共同推进的
那个"远处目标"什么时候算完)。TodoList 是施工排期表,目标契约是
甲方验收单 —— 两者配合:每轮用 TodoList 干活,用 check_goal 向甲方汇报。

依赖方向说明(goal.py 顶部导入 agent.py 是安全的):
  agent.py(工具循环)不依赖 goal.py,tools.py 对 goal 只在
  execute_tool 函数体内延迟导入 —— 不构成循环依赖。
"""
import json
from datetime import datetime

from .config import MEMORY_DIR, GOAL_LOG_PATH, AGENT_NAME
from .agent import chat_with_tools
from .prompt import print_alex

# 当前目标契约(None = 没有活跃目标)
GOAL_STATE: dict | None = None

# 契约允许的状态机:in_progress → done / blocked / failed
GOAL_STATUSES = ("in_progress", "done", "blocked", "failed")


def _append_goal_log(entry: dict):
    """把一条 checkpoint 追加落盘 goal_log.jsonl(第 12 期审计日志的任务版)。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with GOAL_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---- 目标驱动工具实现(暴露给 LLM 的 3 个) ----
def set_goal(goal: str, success_criteria: list[str],
             max_rounds: int = 3) -> str:
    """订目标契约:把模糊目标改写成"目标 + 验收标准 + 预算"。

    这是控制循环的"锚":之后每一轮执行、每一次自评,都对照这份契约。
    success_criteria 必须可逐条判定(写"结构完整"而不是"更好")。
    """
    global GOAL_STATE
    if not goal or not isinstance(success_criteria, list) or not success_criteria:
        return json.dumps({
            "error": "goal 必填,success_criteria 必须是非空列表",
            "hint": "验收标准要可判定,如'方案包含逐节修改建议'",
        }, ensure_ascii=False)
    max_rounds = max(1, min(int(max_rounds), 10))  # 预算硬上限 10 轮
    GOAL_STATE = {
        "goal": goal,
        "success_criteria": success_criteria,
        "max_rounds": max_rounds,
        "status": "in_progress",
        "created_ts": datetime.now().isoformat(timespec="seconds"),
        "checkpoints": [],
    }
    _append_goal_log({
        "ts": GOAL_STATE["created_ts"],
        "event": "contract",
        "goal": goal,
        "success_criteria": success_criteria,
        "max_rounds": max_rounds,
    })
    print(f"  [目标契约] {goal}")
    for i, c in enumerate(success_criteria, 1):
        print(f"             验收{i}: {c}")
    print(f"             预算: 最多 {max_rounds} 轮")
    return json.dumps({
        "contract": "已订立",
        "goal": goal,
        "success_criteria": success_criteria,
        "max_rounds": max_rounds,
        "status": "in_progress",
        "hint": "接下来每轮推进后,必须用 check_goal 对照验收标准自评",
    }, ensure_ascii=False)


def check_goal(assessment: str, new_status: str, evidence: str = "") -> str:
    """打 checkpoint:对照验收标准自评一次,返回判定并落盘。

    控制循环靠它决定"继续还是停":
      done     → 验收达标,循环终止(正常完成)
      blocked  → 受阻(缺权限/缺信息),循环终止(提前止损)
      failed   → 明确做不成,循环终止
      in_progress → 还没完,继续下一轮
    """
    global GOAL_STATE
    if GOAL_STATE is None:
        return json.dumps({"error": "还没有目标契约,先调用 set_goal"}, ensure_ascii=False)
    if new_status not in GOAL_STATUSES:
        return json.dumps({
            "error": f"new_status 必须是 {'/'.join(GOAL_STATUSES)}",
        }, ensure_ascii=False)
    round_no = len(GOAL_STATE["checkpoints"]) + 1
    checkpoint = {
        "round": round_no,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "assessment": assessment,
        "evidence": evidence,
        "status": new_status,
    }
    GOAL_STATE["checkpoints"].append(checkpoint)
    GOAL_STATE["status"] = new_status
    _append_goal_log({
        "ts": checkpoint["ts"],
        "event": "checkpoint",
        "goal": GOAL_STATE["goal"],
        "round": round_no,
        "assessment": assessment,
        "evidence": evidence,
        "status": new_status,
    })
    print(f"  [checkpoint 第{round_no}轮] 状态={new_status}")
    print(f"             自评: {assessment[:100]}")
    return json.dumps({
        "checkpoint": checkpoint,
        "budget_left": GOAL_STATE["max_rounds"] - round_no,
        "hint": ("预算还剩 {} 轮".format(GOAL_STATE["max_rounds"] - round_no)
                 if new_status == "in_progress" else "循环将终止"),
    }, ensure_ascii=False)


def goal_status() -> str:
    """查看目标进度:契约 + checkpoint 历史 + 剩余预算。"""
    if GOAL_STATE is None:
        return json.dumps({
            "error": "当前没有活跃目标",
            "hint": "给一个模糊目标,用 set_goal 订契约",
        }, ensure_ascii=False)
    return json.dumps({
        "goal": GOAL_STATE["goal"],
        "success_criteria": GOAL_STATE["success_criteria"],
        "max_rounds": GOAL_STATE["max_rounds"],
        "status": GOAL_STATE["status"],
        "checkpoints": GOAL_STATE["checkpoints"],
        "budget_left": GOAL_STATE["max_rounds"] - len(GOAL_STATE["checkpoints"]),
        "log_file": str(GOAL_LOG_PATH),
    }, ensure_ascii=False)


def print_goal_panel():
    """目标面板:当前契约 + checkpoint 摘要(启动/每轮后打印)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│ " + "🎯 目标面板(第 14 期)".ljust(57) + "│")
    if GOAL_STATE is None:
        print("│ " + "(无活跃目标 — 给一个模糊目标即可启动)".ljust(57) + "│")
        print(f"│ checkpoint 日志: {GOAL_LOG_PATH.name}(文件不存在则尚未开始)".ljust(58) + "│")
        print("└" + "─" * 58 + "┘")
        return
    goal_line = GOAL_STATE["goal"]
    if len(goal_line) > 44:
        goal_line = goal_line[:44] + "..."
    print("│ 目标: " + goal_line.ljust(50) + "│")
    print("│ 状态: " + GOAL_STATE["status"].ljust(50) + "│")
    used = len(GOAL_STATE["checkpoints"])
    print(f"│ 预算: {used}/{GOAL_STATE['max_rounds']} 轮(剩余 {GOAL_STATE['max_rounds'] - used} 轮)".ljust(58) + "│")
    for c in GOAL_STATE["checkpoints"]:
        mark = {"done": "✅", "blocked": "⛔", "failed": "❌", "in_progress": "⏳"}[c["status"]]
        line = f" {mark} 第{c['round']}轮 [{c['status']}] {c['assessment'][:34]}"
        print(("│ " + line).ljust(58) + "│")
    print(f"│ 验收标准:".ljust(58) + "│")
    for i, crit in enumerate(GOAL_STATE["success_criteria"], 1):
        print(("│   " + f"{i}. {crit}"[:52]).ljust(58) + "│")
    print("└" + "─" * 58 + "┘")


def run_goal_loop(fuzzy_goal: str, session_id: str,
                  system_prompt: str, budget_rounds: int | None = None):
    """第 14 期核心:控制循环 —— 订约 → 执行 → checkpoint → 判停。

    外层循环(run_goal_loop)管"推进和停止",内层循环(chat_with_tools)
    管"这一轮怎么干活"—— 一个循环套一个循环:
      外层每轮:
        1. 塞一条"控制指令"进 messages(告诉 Agent 目标和剩余预算)
        2. 内层 chat_with_tools 让它用工具干活(TodoList/MCP/子代理...)
        3. 干完必须 check_goal 自评 → 契约状态更新
        4. 看契约状态决定外层是否继续:
           done 达标停 / blocked 受阻停 / failed 失败停
      外层退出后若仍是 in_progress → 标记 failed(预算耗尽,封顶止损)

    budget_rounds:可选,外部强制收紧预算(演示用),不传则用契约里的。
    """
    global GOAL_STATE
    print("\n" + "═" * 60)
    print(f"[控制循环] 模糊目标进场: “{fuzzy_goal}”")
    print("═" * 60)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    # ---- Phase 1:订约(把模糊目标改写成结构化契约) ----
    messages.append({"role": "user", "content": (
        f"目标:{fuzzy_goal}\n"
        "这是一个模糊目标。请调用 set_goal 把它订成目标契约:\n"
        "- goal:一句话说清做什么\n"
        "- success_criteria:2-4 条可逐条判定的验收标准(不要写'更好'这种模糊词)\n"
        "- max_rounds:预算轮数(建议 2-3 轮)\n"
        "订完契约后,先用 todo_create 拆出本轮要做的步骤,再开始执行第一步。"
    )})
    answer, messages, p, c = chat_with_tools(messages, session_id)
    if GOAL_STATE is None:
        print("[控制循环] Agent 未订契约,目标驱动模式退出(退回被动应答)")
        return
    if budget_rounds is not None:
        GOAL_STATE["max_rounds"] = min(GOAL_STATE["max_rounds"], budget_rounds)

    # ---- Phase 2:控制循环(执行 → checkpoint → 判停) ----
    max_rounds = GOAL_STATE["max_rounds"]
    for round_no in range(1, max_rounds + 1):
        if GOAL_STATE["status"] != "in_progress":
            break  # 上一轮已判 done/blocked/failed → 不再进轮
        print(f"\n[控制循环 第 {round_no}/{max_rounds} 轮] 继续朝目标推进:")
        messages.append({"role": "user", "content": (
            f"[控制循环 第 {round_no}/{max_rounds} 轮]\n"
            f"当前目标:{GOAL_STATE['goal']}\n"
            f"验收标准:{json.dumps(GOAL_STATE['success_criteria'], ensure_ascii=False)}\n"
            "继续推进目标(可用 todo 更新步骤、可派子代理、可查 MCP)。"
            "本轮结束时必须调用 check_goal,对照验收标准如实自评:"
            "全部达标→done;缺东西做不下去→blocked;确信做不成→failed;还没完→in_progress。"
        )})
        answer, messages, p, c = chat_with_tools(messages, session_id)
        # 与自由对话的 "Alex: ..." 同款人设前缀 —— 一眼认出这是它说的话
        # (轮数由 [控制循环 第 N/M 轮] 行宣布,前缀不再重复)
        print_alex(f"\n{AGENT_NAME}: {answer}")
        if GOAL_STATE["status"] == "in_progress" and round_no == max_rounds:
            # 预算用完仍未达标 → 封顶止损,标记 failed
            check_goal(
                assessment="预算(轮数)耗尽,验收未全部达标",
                new_status="failed",
                evidence=f"控制循环跑满 {max_rounds} 轮,状态仍为 in_progress",
            )

    # ---- Phase 3:结算 + 最终验收汇报(无论成败,契约 + checkpoint 都留档) ----
    print("\n[控制循环] 结束,目标结算:")
    print_goal_panel()
    if GOAL_STATE["status"] == "done":
        print("[结算] ✅ 验收达标 — Agent 自主完成目标")
    elif GOAL_STATE["status"] == "blocked":
        print("[结算] ⛔ 受阻止损 — 缺少前置条件,停下来等人类决策")
    else:
        print("[结算] ❌ 预算耗尽/失败 — checkpoint 日志可复盘卡点")
    print(f"[checkpoint 日志] {GOAL_LOG_PATH}(每轮自评已落盘,可回放)")

    # ---- Phase 3b:最终验收汇报 —— 对照契约逐条交代产出 ----
    # 与"每轮汇报"的区别:每轮汇报说"我做了什么",最终汇报对照验收标准
    # 逐条说"是否达标、产出在哪里" —— 模糊目标进去,一份验收报告出来。
    criteria = GOAL_STATE["success_criteria"]
    criteria_text = "\n".join(f"  {i}. {c}" for i, c in enumerate(criteria, 1))
    messages.append({"role": "user", "content": (
        f"目标已结束(状态:{GOAL_STATE['status']})。请给出最终验收汇报:\n"
        f"对照以下 {len(criteria)} 条验收标准,逐条说明是否达标、产出在哪里:\n"
        f"{criteria_text}\n"
        "最后一句话总结本次目标的整体结果。不要再调用工具,直接汇报。"
    )})
    try:
        answer, messages, p, c = chat_with_tools(messages, session_id)
        print_alex(f"\n{AGENT_NAME}: {answer}")
    except Exception as e:
        print(f"[最终验收汇报] 生成失败: {e}")
