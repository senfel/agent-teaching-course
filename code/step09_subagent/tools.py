"""tools.py — 工具定义(TOOLS) + 工具调度(execute_tool)

[第 01 期] 2 个基础工具:get_current_time / calculate
[第 04 期] 1 个技能工具:load_skill
[第 06 期] 2 个记忆工具:save_user_profile / recall_memory(实现委托 memory.py)
[第 08 期] 3 个任务工具:todo_create / todo_update / todo_list(实现委托 todo.py)
[第 09 期] 3 个子代理工具:dispatch_subagent / dispatch_subagents_parallel / list_subagents(实现委托 subagent.py)
合计 11 个工具。execute_tool 只做调度,具体实现委托给各领域模块。
"""
import json
import ast
import operator
from datetime import datetime

from .config import SKILLS_DIR
from .prompt import load_skills_index
from .memory import save_user_profile, recall_memory
from .todo import todo_create, todo_update, todo_list


# ============ 工具定义(11 个)============
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。当用户问'现在几点''今天日期'等问题时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "description": "目标时区,默认 Asia/Shanghai。"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "进行四则运算。当用户要求计算数学表达式时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "数学表达式,如 '17 * 23'"}
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "加载一个技能的完整内容。当任务与技能索引中某个技能相关时,"
                "先调用此工具拿到技能全文,再按技能内容回答。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名,如 weather、git-cheatsheet"}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_user_profile",
            "description": (
                "更新用户画像文件 templates/USER.md。当用户表达了稳定偏好、"
                "个人信息变更时调用。不要在每次对话后都调用,只在偏好真正变化时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "description": "要更新的章节名,如 '稳定偏好'"},
                    "new_content": {"type": "string", "description": "该章节的新内容(完整替换该章节)"},
                },
                "required": ["section", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "查询 Agent 记得的关于用户的信息。返回用户画像内容。"
                "当用户问'你还记得我吗''我之前说过什么'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "查询主题,可省略,返回全部画像。"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_create",
            "description": (
                "把一个模糊需求拆成可执行的子任务列表。"
                "当用户提出复杂任务(如'帮我准备周会''帮我做代码审查')时,"
                "先用此工具拆解成 3-7 个具体子任务,再逐个执行。"
                "所有任务初始状态为 pending。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "子任务内容列表,如 ['收集本周工作要点', '整理待讨论议题', '写周报草稿']",
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_update",
            "description": (
                "更新任务状态。状态可选:pending(待办)、in_progress(进行中)、completed(已完成)。"
                "约束 1(顺序):设为 in_progress 时,排在前面的任务必须全部 completed,不能跳过。"
                "约束 2(单 in_progress):同一时刻只能有一个任务处于 in_progress,"
                "设新的会自动把之前的 in_progress 退回 pending。"
                "工作流:按顺序逐个推进 #1→#2→#3,开始时 in_progress,完成后 completed,再做下一个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "要更新的任务 ID"},
                    "new_status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                        "description": "新状态",
                    },
                },
                "required": ["task_id", "new_status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_list",
            "description": (
                "查看当前任务列表及进度统计。返回各任务的状态和完成率。"
                "当你需要回顾当前进度、确认下一步做什么时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # ↓↓↓ 本期新增:子代理工具 ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "dispatch_subagent",
            "description": (
                "派遣一个子代理执行特定任务。子代理有独立上下文(不复用主 Agent 对话历史)"
                "和工具白名单(只能用部分工具)。"
                "当任务需要某个专家角色(如代码审查、文档整理)时调用。"
                "可用类型:code_reviewer(代码审查员)、doc_writer(文档整理员)。"
                "注意:子代理不能用 todo、不能用 save_user_profile、不能派遣其他子代理。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_type": {
                        "type": "string",
                        "enum": ["code_reviewer", "doc_writer"],
                        "description": "子代理类型",
                    },
                    "task": {"type": "string", "description": "要交给子代理的任务描述"},
                },
                "required": ["agent_type", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_subagents_parallel",
            "description": (
                "并发派遣多个子代理。所有子代理同时开始执行,总耗时约等于最慢的那个。"
                "当需要多个专家同时工作时调用(如'审查代码 + 整理文档')。"
                "tasks 是一个列表,每项包含 agent_type 和 task。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_type": {
                                    "type": "string",
                                    "enum": ["code_reviewer", "doc_writer"],
                                    "description": "子代理类型",
                                },
                                "task": {"type": "string", "description": "任务描述"},
                            },
                            "required": ["agent_type", "task"],
                        },
                        "description": "子代理任务列表",
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_subagents",
            "description": "查看可用的子代理类型及其工具白名单。当不确定有哪些子代理时调用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


# ============ 工具实现 ============
_ALLOWED_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node):
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

    if name == "load_skill":
        skill_name = arguments.get("name", "")
        skill_md = SKILLS_DIR / skill_name / "SKILL.md"
        if not skill_md.exists():
            available = ", ".join(s["name"] for s in load_skills_index())
            return json.dumps({
                "error": f"技能不存在: {skill_name}",
                "available": available,
            }, ensure_ascii=False)
        return json.dumps({
            "skill": skill_name,
            "content": skill_md.read_text(encoding="utf-8"),
        }, ensure_ascii=False)

    if name == "save_user_profile":
        section = arguments.get("section", "")
        new_content = arguments.get("new_content", "")
        return save_user_profile(section, new_content)

    if name == "recall_memory":
        topic = arguments.get("topic", "").strip()
        return recall_memory(topic)

    # ↓↓↓ 第 08 期:TodoList 工具 ↓↓↓
    if name == "todo_create":
        tasks = arguments.get("tasks", [])
        if not tasks or not isinstance(tasks, list):
            return json.dumps({"error": "tasks 必须是非空列表"}, ensure_ascii=False)
        return todo_create(tasks)

    if name == "todo_update":
        task_id = arguments.get("task_id")
        new_status = arguments.get("new_status", "")
        if task_id is None:
            return json.dumps({"error": "task_id 必填"}, ensure_ascii=False)
        return todo_update(int(task_id), new_status)

    if name == "todo_list":
        return todo_list()

    # ↓↓↓ 本期新增:Subagent 工具(延迟导入,避免循环依赖)↓↓↓
    if name == "dispatch_subagent":
        from .subagent import dispatch_subagent as _dispatch
        agent_type = arguments.get("agent_type", "")
        task = arguments.get("task", "")
        if not agent_type or not task:
            return json.dumps({"error": "agent_type 和 task 必填"}, ensure_ascii=False)
        return _dispatch(agent_type, task)

    if name == "dispatch_subagents_parallel":
        from .subagent import dispatch_subagents_parallel as _dispatch_parallel
        tasks = arguments.get("tasks", [])
        return _dispatch_parallel(tasks)

    if name == "list_subagents":
        from .subagent import list_subagents as _list_subagents
        return _list_subagents()

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
