"""todo.py — TodoList 任务规划层

[第 08 期] 三态机:pending → in_progress → completed
[第 08 期] 双约束:顺序约束(前面的必须完成) + 单 in_progress(同一时刻只有一个进行中)
"""
import json
from datetime import datetime

# 任务状态常量
TODO_PENDING = "pending"
TODO_IN_PROGRESS = "in_progress"
TODO_COMPLETED = "completed"

# 运行时内存(纯内存,不持久化,用完即弃)
_todo_list: list[dict] = []
# 每个 task: {"id": int, "content": str, "status": str, "created_at": str}


def _next_todo_id(tasks: list[dict]) -> int:
    """生成下一个任务 ID(从 1 开始,单调递增)。"""
    if not tasks:
        return 1
    return max(t["id"] for t in tasks) + 1


def _todo_snapshot(tasks: list[dict]) -> list[dict]:
    """返回任务列表的快照(精简版)。"""
    return [{"id": t["id"], "content": t["content"], "status": t["status"]} for t in tasks]


def todo_create(tasks: list[str]) -> str:
    """批量创建待办任务。

    约束:创建时全部为 pending,后续通过 todo_update 逐个推进。
    """
    created = []
    for content in tasks:
        task = {
            "id": _next_todo_id(_todo_list),
            "content": content,
            "status": TODO_PENDING,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        _todo_list.append(task)
        created.append(task)
    return json.dumps({
        "created": len(created),
        "tasks": [{"id": t["id"], "content": t["content"], "status": t["status"]} for t in created],
    }, ensure_ascii=False)


def todo_update(task_id: int, new_status: str) -> str:
    """更新任务状态,强制两条约束:

    约束 1(顺序):设为 in_progress 时,排在前面的任务必须全部 completed。
                   不允许跳过 #1 直接开 #3,强制一条条往下执行。
    约束 2(单 in_progress):同一时刻只能有一个任务处于 in_progress。
                   设新的 in_progress 时,旧的自动退回 pending。
    """
    # 校验状态值
    valid_statuses = {TODO_PENDING, TODO_IN_PROGRESS, TODO_COMPLETED}
    if new_status not in valid_statuses:
        return json.dumps({"error": f"无效状态: {new_status}", "valid": list(valid_statuses)}, ensure_ascii=False)

    # 找到目标任务
    target = None
    for t in _todo_list:
        if t["id"] == task_id:
            target = t
            break
    if target is None:
        return json.dumps({"error": f"任务不存在: id={task_id}"}, ensure_ascii=False)

    # 约束 1:顺序约束 —— 前面的任务必须全部完成才能开始
    if new_status == TODO_IN_PROGRESS:
        blocked_by = []
        for t in _todo_list:
            if t["id"] < task_id and t["status"] != TODO_COMPLETED:
                blocked_by.append(t["id"])
        if blocked_by:
            return json.dumps({
                "error": f"顺序约束:任务 #{task_id} 前面还有未完成任务 {blocked_by}",
                "hint": "必须按顺序逐个完成,不能跳过",
                "blocked_by": blocked_by,
                "current_list": _todo_snapshot(_todo_list),
            }, ensure_ascii=False)

    # 约束 2:单 in_progress 约束 —— 把其他 in_progress 的退回 pending
    if new_status == TODO_IN_PROGRESS:
        for t in _todo_list:
            if t["status"] == TODO_IN_PROGRESS and t["id"] != task_id:
                t["status"] = TODO_PENDING
                print(f"  [todo] 任务 #{t['id']} 自动从 in_progress 退回 pending(单 in_progress 约束)")

    target["status"] = new_status

    # 全部完成后自动清空,回到初始状态(用完即弃)
    if new_status == TODO_COMPLETED and all(t["status"] == TODO_COMPLETED for t in _todo_list):
        completed_snapshot = _todo_snapshot(_todo_list)
        _todo_list.clear()
        print("  [todo] 所有任务已完成,列表已清空")
        return json.dumps({
            "updated": task_id,
            "new_status": new_status,
            "all_completed": True,
            "cleared": True,
            "completed_tasks": completed_snapshot,
        }, ensure_ascii=False)

    return json.dumps({
        "updated": task_id,
        "content": target["content"],
        "new_status": new_status,
        "current_list": _todo_snapshot(_todo_list),
    }, ensure_ascii=False)


def todo_list() -> str:
    """查看当前任务列表及进度。"""
    return json.dumps({
        "total": len(_todo_list),
        "pending": sum(1 for t in _todo_list if t["status"] == TODO_PENDING),
        "in_progress": sum(1 for t in _todo_list if t["status"] == TODO_IN_PROGRESS),
        "completed": sum(1 for t in _todo_list if t["status"] == TODO_COMPLETED),
        "tasks": _todo_snapshot(_todo_list),
    }, ensure_ascii=False)


def print_todo_panel():
    """打印 TodoList 面板(教学可视化)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  📋 Alex 任务面板(第 08 期)                            │")
    print("├" + "─" * 58 + "┤")

    if not _todo_list:
        print("│  (空)还没有任务,让 Alex 帮你规划                     │")
    else:
        for t in _todo_list:
            icon = {"pending": "⬚", "in_progress": "▶", "completed": "✓"}[t["status"]]
            line = f"  {icon} #{t['id']} {t['content']}"
            if len(line) > 54:
                line = line[:51] + "..."
            print(f"│{line:<56}│")

        # 进度统计
        total = len(_todo_list)
        done = sum(1 for t in _todo_list if t["status"] == TODO_COMPLETED)
        if total > 0:
            bar_len = 40
            filled = int(bar_len * done / total)
            bar = "█" * filled + "░" * (bar_len - filled)
            pct = done * 100 // total
            print("│" + "─" * 58 + "│")
            print(f"│  {bar} {pct:3d}% ({done}/{total})                          │")
    print("└" + "─" * 58 + "┘")
    print()
