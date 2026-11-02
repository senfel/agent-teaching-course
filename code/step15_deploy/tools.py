"""tools.py — 工具定义(TOOLS) + 工具调度(execute_tool)

[第 01 期] 2 个基础工具:get_current_time / calculate
[第 04 期] 1 个技能工具:load_skill
[第 06 期] 2 个记忆工具:save_user_profile / recall_memory(实现委托 memory.py)
[第 08 期] 3 个任务工具:todo_create / todo_update / todo_list(实现委托 todo.py)
[第 09 期] 3 个子代理工具:dispatch_subagent / dispatch_subagents_parallel / list_subagents(实现委托 subagent.py)
[第 10 期] 5 个团队工具:team_wake / send_message / broadcast_message / read_inbox / list_team(实现委托 team.py)
[第 11 期] 4 个 MCP 工具:list_mcp_servers / mcp_connect / mcp_list_tools / mcp_call_tool(实现委托 mcp.py)
[第 12 期] 2 个 Hooks 工具:list_hooks / view_audit_log(实现委托 hooks.py)
[第 13 期] 3 个模型路由工具:list_models / switch_model / cost_report(实现委托 model_router.py)
[第 14 期] 3 个目标驱动工具:set_goal / check_goal / goal_status(实现委托 goal.py)
合计 28 个工具。execute_tool 只做调度,具体实现委托给各领域模块。

依赖方向说明(为什么 subagent / team / mcp / hooks / model_router 的导入
要写在 execute_tool 函数体内,而不是文件顶部):
  这些模块反过来要用 tools.py 的 TOOLS / execute_tool(如 subagent 的工具
  白名单要过滤 TOOLS,队友循环要调 execute_tool),如果互相在顶部 import
  就会形成循环依赖。函数体内"用到才导入"是 Python 惯用的解法。
"""
import json
import ast
import operator
from datetime import datetime

from .config import SKILLS_DIR
from .prompt import load_skills_index
from .memory import save_user_profile, recall_memory
from .todo import todo_create, todo_update, todo_list


# ============ 工具定义(25 个)============
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
    # ↓↓↓ 第 09 期:子代理工具 ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "dispatch_subagent",
            "description": (
                "派遣一个子代理执行特定任务。子代理有独立上下文(不复用主 Agent 对话历史)"
                "和工具白名单(只能用部分工具)。子代理是临时的,用完即弃。"
                "当任务需要某个专家角色(如代码审查、文档整理)且不需要跨会话保持状态时调用。"
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
    # ↓↓↓ 第 10 期新增:Agent Team 工具(本期沿用) ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "team_wake",
            "description": (
                "唤醒一个持久队友执行任务。队友的对话历史和 inbox 跨唤醒保持"
                "(不像子代理用完即弃)。唤醒时,队友会自动读取 inbox 里的未读消息。"
                "适用场景:需要多个角色多轮协作(如前端+后端协作开发功能)时使用。"
                "可用成员:frontend_dev(前端开发)、backend_dev(后端开发)。"
                "队友之间可以通过 send_message / broadcast_message 互相通信。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "member_id": {
                        "type": "string",
                        "enum": ["frontend_dev", "backend_dev"],
                        "description": "要唤醒的团队成员 ID",
                    },
                    "task": {"type": "string", "description": "交给队友的任务描述"},
                },
                "required": ["member_id", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "给指定队友的 inbox 发送一条消息。消息进入对方 inbox,"
                "对方下次被 team_wake 唤醒时会自动看到。"
                "主 Agent 和队友都能调用(队友调用时发送者为自己)。"
                "适用场景:前端队友需要接口定义时给后端队友发消息。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_member": {
                        "type": "string",
                        "enum": ["frontend_dev", "backend_dev"],
                        "description": "接收消息的队友 ID",
                    },
                    "content": {"type": "string", "description": "消息内容"},
                },
                "required": ["to_member", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "broadcast_message",
            "description": (
                "向所有队友的 inbox 广播一条消息。一条消息同时进入所有队友的 inbox。"
                "适用场景:需求变更通知、全局进度同步等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "广播消息内容"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_inbox",
            "description": (
                "读取某个队友的 inbox 消息(不消费,仅查看)。"
                "当你想了解队友收到了哪些消息时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "member_id": {
                        "type": "string",
                        "enum": ["frontend_dev", "backend_dev"],
                        "description": "要查看 inbox 的队友 ID",
                    },
                },
                "required": ["member_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_team",
            "description": (
                "查看团队成员列表及其状态(inbox 未读消息数、唤醒次数、是否有持久上下文)。"
                "当不确定团队里有哪些成员时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # ↓↓↓ 第 11 期新增:MCP 工具(本期沿用) ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "list_mcp_servers",
            "description": (
                "查看有哪些 MCP 服务器可连接(名称、描述、连接状态、工具数量)。"
                "当用户提到需要外部工具能力(如查仓库、查数据库)时,先调用此工具看看有什么服务器。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_connect",
            "description": (
                "连接一个 MCP 服务器(启动服务器子进程 + initialize 握手 + 拉取工具清单)。"
                "调用 MCP 服务器上的工具之前,一般先连接它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "enum": ["repo"],
                        "description": "MCP 服务器 ID,如 repo",
                    },
                },
                "required": ["server_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_list_tools",
            "description": (
                "列出某个 MCP 服务器提供的工具(名称、描述、参数 schema)。"
                "调用 MCP 工具前,先看它叫什么名字、要传什么参数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "MCP 服务器 ID,如 repo",
                    },
                },
                "required": ["server_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_call_tool",
            "description": (
                "调用 MCP 服务器上的一个工具。这是访问外部工具服务器的统一入口:"
                "传服务器 ID + 工具名 + 参数 JSON,返回工具的文本结果。"
                "用法示例:mcp_call_tool('repo', 'get_repository_info', "
                "{\"owner\": \"deepseek-ai\", \"repo\": \"DeepSeek-V3\"})"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "MCP 服务器 ID,如 repo",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "服务器上的工具名,如 get_repository_info",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "工具参数(JSON 对象),具体结构见 mcp_list_tools 的 inputSchema",
                    },
                },
                "required": ["server_id", "tool_name", "arguments"],
            },
        },
    },
    # ↓↓↓ 第 12 期新增:Hooks 工具(本期沿用) ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "list_hooks",
            "description": (
                "查看当前注册的 Hooks(工具调用前后的钩子链):每个钩子的阶段"
                "(before/after)、名称、类型(拦截/改写/审计)、适用范围,以及敏感词表和保护工具名单。"
                "当用户想知道'你的工具调用有什么安全机制'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_audit_log",
            "description": (
                "查看操作审计日志(最近 N 条):每次工具调用的 时间/工具名/状态"
                "(ok 正常、masked 参数被打码、blocked 被拦截)/参数摘要/结果摘要/耗时,"
                "以及整体统计。当用户想回放'Agent 刚才都做了什么'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "返回最近多少条,默认 20",
                    },
                },
                "required": [],
            },
        },
    },
    # ↓↓↓ 第 13 期新增:多模型路由工具 ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "list_models",
            "description": (
                "查看多模型注册表:每个模型的 档位(light 轻量/heavy 重型)/"
                "输入输出单价(美元每百万 token)/强项/底层 API 模型,"
                "以及当前路由模式(auto 自动/manual 手动钉死)和最近的路由决策记录。"
                "当用户问'你都有哪些模型可用''现在用的是哪个模型'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_model",
            "description": (
                "切换路由模式或手动指定模型。三种用法:"
                "① switch_model(mode='auto') 恢复自动路由(按任务复杂度选档);"
                "② switch_model(mode='manual', model_id='claude-sonnet') 钉死某个模型;"
                "③ switch_model(mode='auto', task='background') 临时指定后台任务用轻量模型。"
                "当用户说'用便宜点的模型''接下来都用 claude 回答'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "manual"],
                        "description": "路由模式:auto=自动按复杂度选档,manual=手动钉死模型",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "manual 模式下要钉死的模型 ID,如 deepseek-chat/claude-sonnet/local-qwen",
                    },
                    "task": {
                        "type": "string",
                        "description": "临时指定某类任务用的模型,如 background(后台任务)",
                    },
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cost_report",
            "description": (
                "查看成本 telemetry 报告:每个模型消耗的 token 数和费用(美元),"
                "按任务类别(main 主对话/compact 压缩/subagent 子代理/team 团队)分组统计,"
                "给出最贵的一次调用和省钱建议。当用户问'这次对话花了多少钱'"
                "'哪个模型最贵'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # ↓↓↓ 本期新增:目标驱动工具(第 14 期) ↓↓↓
    {
        "type": "function",
        "function": {
            "name": "set_goal",
            "description": (
                "订立目标契约:把用户的模糊目标(如'帮我优化这个仓库的 README')"
                "改写成结构化契约 —— goal(一句话说清做什么)+ success_criteria"
                "(2-4 条可逐条判定的验收标准,不要写'更好'这种模糊词)"
                "+ max_rounds(预算轮数上限)。"
                "当用户提出一个需要多轮推进才能完成的目标时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "一句话说清要做什么,如'产出本仓库 README 的逐节优化方案'",
                    },
                    "success_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-4 条验收标准,每条必须可独立判定是否达标",
                    },
                    "max_rounds": {
                        "type": "integer",
                        "description": "预算:最多允许的控制循环轮数,建议 2-3 轮,硬上限 10 轮",
                    },
                },
                "required": ["goal", "success_criteria"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_goal",
            "description": (
                "打 checkpoint:对照目标契约的验收标准自评一次。"
                "每轮工作结束时必须调用,状态决定控制循环是否继续:"
                "done=全部达标(循环停止);blocked=缺权限/缺信息做不下去(循环停止);"
                "failed=确信做不成(循环停止);in_progress=还没完(进入下一轮)。"
                "必须如实自评,禁止未达标就报 done。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "assessment": {
                        "type": "string",
                        "description": "本轮自评:对照每条验收标准,哪些达标了、哪些没有",
                    },
                    "new_status": {
                        "type": "string",
                        "enum": ["in_progress", "done", "blocked", "failed"],
                        "description": "自评后的契约状态",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "支撑自评的证据(做了什么/产出在哪里),可选",
                    },
                },
                "required": ["assessment", "new_status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_status",
            "description": (
                "查看目标进度:当前目标契约(目标/验收标准/预算)"
                "、checkpoint 自评历史(每一轮的状态和依据)、剩余预算轮数。"
                "当用户问'目标做到哪了''还剩几轮预算'时调用。"
            ),
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

    # ↓↓↓ 第 09 期:Subagent 工具(延迟导入,避免循环依赖)↓↓↓
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

    # ↓↓↓ 第 10 期新增:Agent Team 工具(本期沿用,延迟导入避免循环依赖)↓↓↓
    if name == "team_wake":
        from .team import team_wake as _team_wake
        member_id = arguments.get("member_id", "")
        task = arguments.get("task", "")
        if not member_id or not task:
            return json.dumps({"error": "member_id 和 task 必填"}, ensure_ascii=False)
        return _team_wake(member_id, task)

    if name == "send_message":
        from .team import send_message as _send_message
        to_member = arguments.get("to_member", "")
        content = arguments.get("content", "")
        if not to_member or not content:
            return json.dumps({"error": "to_member 和 content 必填"}, ensure_ascii=False)
        return _send_message(to_member, content)

    if name == "broadcast_message":
        from .team import broadcast_message as _broadcast_message
        content = arguments.get("content", "")
        if not content:
            return json.dumps({"error": "content 必填"}, ensure_ascii=False)
        return _broadcast_message(content)

    if name == "read_inbox":
        from .team import read_inbox as _read_inbox
        member_id = arguments.get("member_id", "")
        if not member_id:
            return json.dumps({"error": "member_id 必填"}, ensure_ascii=False)
        return _read_inbox(member_id)

    if name == "list_team":
        from .team import list_team as _list_team
        return _list_team()

    # ↓↓↓ 第 11 期新增:MCP 工具(本期沿用,延迟导入避免循环依赖)↓↓↓
    if name == "list_mcp_servers":
        from .mcp import list_mcp_servers as _list_mcp_servers
        return _list_mcp_servers()

    if name == "mcp_connect":
        from .mcp import mcp_connect as _mcp_connect
        server_id = arguments.get("server_id", "")
        if not server_id:
            return json.dumps({"error": "server_id 必填"}, ensure_ascii=False)
        return _mcp_connect(server_id)

    if name == "mcp_list_tools":
        from .mcp import mcp_list_tools as _mcp_list_tools
        server_id = arguments.get("server_id", "")
        if not server_id:
            return json.dumps({"error": "server_id 必填"}, ensure_ascii=False)
        return _mcp_list_tools(server_id)

    if name == "mcp_call_tool":
        from .mcp import mcp_call_tool as _mcp_call_tool
        server_id = arguments.get("server_id", "")
        tool_name = arguments.get("tool_name", "")
        mcp_args = arguments.get("arguments", {})
        if not server_id or not tool_name:
            return json.dumps({"error": "server_id 和 tool_name 必填"}, ensure_ascii=False)
        if not isinstance(mcp_args, dict):
            return json.dumps({"error": "arguments 必须是 JSON 对象"}, ensure_ascii=False)
        return _mcp_call_tool(server_id, tool_name, mcp_args)

    # ↓↓↓ 第 12 期新增:Hooks 工具(本期沿用,延迟导入避免循环依赖)↓↓↓
    if name == "list_hooks":
        from .hooks import list_hooks as _list_hooks
        return _list_hooks()

    if name == "view_audit_log":
        from .hooks import view_audit_log as _view_audit_log
        limit = arguments.get("limit", 20)
        if not isinstance(limit, int) or limit <= 0:
            limit = 20
        return _view_audit_log(limit)

    # ↓↓↓ 第 13 期新增:多模型路由工具(延迟导入避免循环依赖)↓↓↓
    if name == "list_models":
        from .model_router import list_models as _list_models
        return _list_models()

    if name == "switch_model":
        from .model_router import switch_model as _switch_model
        mode = arguments.get("mode", "")
        if mode not in ("auto", "manual"):
            return json.dumps(
                {"error": "mode 必须是 auto 或 manual"}, ensure_ascii=False
            )
        model_id = arguments.get("model_id") or None
        task = arguments.get("task") or None
        return _switch_model(mode=mode, model_id=model_id, task=task)

    if name == "cost_report":
        from .model_router import cost_report as _cost_report
        return _cost_report()

    # ↓↓↓ 第 14 期新增:目标驱动工具(延迟导入避免循环依赖)↓↓↓
    if name == "set_goal":
        from .goal import set_goal as _set_goal
        goal = arguments.get("goal", "")
        criteria = arguments.get("success_criteria", [])
        max_rounds = arguments.get("max_rounds", 3)
        if not isinstance(criteria, list):
            return json.dumps({"error": "success_criteria 必须是列表"}, ensure_ascii=False)
        return _set_goal(goal, criteria, max_rounds)

    if name == "check_goal":
        from .goal import check_goal as _check_goal
        assessment = arguments.get("assessment", "")
        new_status = arguments.get("new_status", "")
        evidence = arguments.get("evidence", "")
        if not assessment or not new_status:
            return json.dumps({"error": "assessment 和 new_status 必填"}, ensure_ascii=False)
        return _check_goal(assessment, new_status, evidence)

    if name == "goal_status":
        from .goal import goal_status as _goal_status
        return _goal_status()

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
