"""team.py — Agent Team 团队协作层(第 10 期核心新增)

[第 10 期] 三个核心概念:
1. 持久队友:team member 的 messages 和 inbox 跨唤醒保持,不像 subagent 用完即弃
   → 前端队友第二次被唤醒时,还记得第一次做了什么
2. inbox 消息系统:每个队友有 inbox(消息队列),队友之间异步通信
   → 前端队友给后端队友发消息"我需要登录接口的格式",后端队友下次被唤醒时看到
3. 消息广播:broadcast_message 一次性给所有队友的 inbox 发消息
   → "大家注意,需求变更了"一条消息所有人收到

与 Subagent 的核心区别:
┌──────────────┬─────────────────────┬──────────────────────────────┐
│              │ Subagent(第09期)    │ Team Member(本期)            │
├──────────────┼─────────────────────┼──────────────────────────────┤
│ 生命周期     │ 临时:派遣→运行→销毁  │ 持久:注册一次,可多次唤醒     │
│ 上下文       │ 每次 dispatch 全新    │ messages 跨唤醒保持(累积)    │
│ 通信         │ 单向(主→子→结果)    │ 双向(inbox 异步消息)        │
│ 状态         │ 无状态                │ 有 inbox + 持久 messages     │
│ 协作         │ 主 Agent 汇总        │ 队友通过 inbox 直接协作       │
└──────────────┴─────────────────────┴──────────────────────────────┘
"""
import json
import time
from datetime import datetime

from .config import client, MODEL
from .prompt import load_system_prompt, load_skills_index, build_skills_prompt
from .tools import TOOLS, execute_tool


# ============ 团队成员注册表:持久队友(有 inbox + 持久 messages + 唤醒计数)============
TEAM_MEMBERS: dict[str, dict] = {
    "frontend_dev": {
        "description": "前端开发工程师。负责页面结构、样式、交互逻辑,能与其他队友通过消息协作。",
        "allowed_tools": ["get_current_time", "calculate", "load_skill",
                          "send_message", "broadcast_message"],
        "extra_prompt": (
            "\n\n## 团队角色:前端开发工程师\n"
            "你是团队的前端开发成员。你的职责:\n"
            "1. 设计页面结构(HTML 骨架)\n"
            "2. 编写样式和交互逻辑\n"
            "3. 与后端队友协作:需要接口时通过 send_message 询问\n"
            "4. 收到后端回复后对接 API\n\n"
            "你可以给其他队友发消息(send_message)或广播(broadcast_message)。\n"
            "你的对话历史会跨唤醒保持,下次被唤醒时还记得之前做了什么。\n"
        ),
        "inbox": [],       # 持久 inbox:其他队友发来的消息
        "messages": None,  # 持久对话历史:None = 尚未被唤醒过
        "wake_count": 0,   # 被唤醒次数
    },
    "backend_dev": {
        "description": "后端开发工程师。负责 API 接口、数据模型、业务逻辑,能与其他队友通过消息协作。",
        "allowed_tools": ["get_current_time", "calculate", "load_skill",
                          "send_message", "broadcast_message"],
        "extra_prompt": (
            "\n\n## 团队角色:后端开发工程师\n"
            "你是团队的后端开发成员。你的职责:\n"
            "1. 设计 API 接口(路由、请求/响应格式)\n"
            "2. 编写数据模型和业务逻辑\n"
            "3. 与前端队友协作:收到询问后通过 send_message 回复接口定义\n"
            "4. 确保接口安全(输入校验、错误处理)\n\n"
            "你可以给其他队友发消息(send_message)或广播(broadcast_message)。\n"
            "你的对话历史会跨唤醒保持,下次被唤醒时还记得之前做了什么。\n"
        ),
        "inbox": [],
        "messages": None,
        "wake_count": 0,
    },
}

# 当前正在运行的队友 ID(用于 send_message 判断发送者)
# 主 Agent 调用 send_message 时,_current_member_id 为 None → 发送者为 "team_lead"
_current_member_id: str | None = None


def _run_team_member(member_id: str, task: str) -> tuple[str, int, int]:
    """执行一个持久队友(持久 messages + inbox 注入 + 工具白名单)。

    与 _run_subagent 的三个关键区别:
    1. 持久上下文:messages 跨唤醒保持(不像 subagent 每次 dispatch 全新)
    2. inbox 注入:唤醒时把未读消息注入到 task 前面,读后清空
    3. 通信工具:队友的 allowed_tools 包含 send_message / broadcast_message

    返回:(结果文本, prompt_tokens, completion_tokens)
    """
    global _current_member_id
    config = TEAM_MEMBERS[member_id]

    # ① 初始化持久 messages(仅第一次唤醒时)
    if config["messages"] is None:
        sub_system = load_system_prompt() + config["extra_prompt"]
        skills_index = load_skills_index()
        skills_prompt = build_skills_prompt(skills_index)
        if skills_prompt:
            sub_system += "\n" + skills_prompt
        config["messages"] = [
            {"role": "system", "content": sub_system},
        ]

    # ② inbox 注入:把未读消息拼到 task 前面,读后清空
    if config["inbox"]:
        inbox_lines = []
        for msg in config["inbox"]:
            inbox_lines.append(f"  [{msg['from']}] {msg['content']}")
        inbox_text = (
            "\n\n--- 收到的队友消息 ---\n"
            + "\n".join(inbox_lines)
            + "\n--- 消息结束 ---"
        )
        config["inbox"].clear()  # 读后清空(已消费)
        print(f"  [team·{member_id}] 读取 {len(inbox_lines)} 条 inbox 消息")
    else:
        inbox_text = ""

    # ③ 追加 task 到持久 messages(上下文累积)
    user_content = task + inbox_text
    config["messages"].append({"role": "user", "content": user_content})
    config["wake_count"] += 1

    # ④ 工具白名单(包含通信工具)
    allowed = set(config["allowed_tools"])
    sub_tools = [t for t in TOOLS if t["function"]["name"] in allowed]

    # ⑤ 设置当前队友 ID(供 send_message 判断发送者)
    _current_member_id = member_id

    # ⑥ 队友工具循环(复用第 05 期循环,但用持久 messages + 受限工具)
    total_prompt = 0
    total_completion = 0

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=config["messages"],
            tools=sub_tools,
            tool_choice="auto",
            max_tokens=2048,
        )
        msg = response.choices[0].message
        total_prompt += response.usage.prompt_tokens
        total_completion += response.usage.completion_tokens

        if msg.tool_calls:
            config["messages"].append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })
            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)
                print(f"    [team·{member_id}] 工具调用: {fn_name}({fn_args})")
                result = execute_tool(fn_name, fn_args)
                display = result if len(result) <= 80 else result[:80] + "..."
                print(f"    [team·{member_id}] 结果: {display}")
                config["messages"].append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
            continue

        config["messages"].append({"role": "assistant", "content": msg.content})

        # 清除当前队友 ID
        _current_member_id = None

        return msg.content, total_prompt, total_completion


def team_wake(member_id: str, task: str) -> str:
    """唤醒一个持久队友执行任务。

    与 dispatch_subagent 的关键区别:
    - subagent:每次 dispatch 全新 messages,用完即弃
    - team member:messages 跨唤醒保持,inbox 消息自动注入

    唤醒时:持久 messages 追加新 task + 未读 inbox 消息,队友用自己的工具循环处理。
    """
    if member_id not in TEAM_MEMBERS:
        return json.dumps({
            "error": f"未知团队成员: {member_id}",
            "available": list(TEAM_MEMBERS.keys()),
        }, ensure_ascii=False)

    config = TEAM_MEMBERS[member_id]
    first_wake = config["messages"] is None
    print(f"  [team] 唤醒 {member_id}({'首次' if first_wake else f'第{config['wake_count']+1}次'})...")

    start = time.time()
    result, p, c = _run_team_member(member_id, task)
    elapsed = time.time() - start

    return json.dumps({
        "member_id": member_id,
        "wake_count": config["wake_count"],
        "result": result,
        "prompt_tokens": p,
        "completion_tokens": c,
        "elapsed_seconds": round(elapsed, 2),
    }, ensure_ascii=False)


def send_message(to_member: str, content: str) -> str:
    """给指定队友的 inbox 发送一条消息。

    主 Agent 和队友都能调用:
    - 主 Agent 调用时:发送者为 "team_lead"
    - 队友调用时:发送者为自己的 member_id(由 _current_member_id 确定)

    消息进入对方的 inbox,对方下次被唤醒时自动看到。
    """
    if to_member not in TEAM_MEMBERS:
        return json.dumps({
            "error": f"未知团队成员: {to_member}",
            "available": list(TEAM_MEMBERS.keys()),
        }, ensure_ascii=False)

    sender = _current_member_id or "team_lead"
    msg_record = {
        "from": sender,
        "content": content,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    TEAM_MEMBERS[to_member]["inbox"].append(msg_record)

    print(f"  [team] {sender} → {to_member} inbox: {content[:60]}")

    return json.dumps({
        "sent": True,
        "from": sender,
        "to": to_member,
        "content": content[:200],
        "inbox_size": len(TEAM_MEMBERS[to_member]["inbox"]),
    }, ensure_ascii=False)


def broadcast_message(content: str) -> str:
    """向所有队友的 inbox 广播一条消息。

    一条消息同时进入所有队友的 inbox。
    适用场景:需求变更、全局通知、进度同步等。
    """
    sender = _current_member_id or "team_lead"
    delivered = []
    for member_id, config in TEAM_MEMBERS.items():
        if member_id == sender:
            continue  # 不给自己发
        config["inbox"].append({
            "from": sender,
            "content": content,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "broadcast": True,
        })
        delivered.append(member_id)

    print(f"  [team] {sender} 广播给 {len(delivered)} 个队友: {content[:60]}")

    return json.dumps({
        "broadcast": True,
        "from": sender,
        "delivered_to": delivered,
        "content": content[:200],
    }, ensure_ascii=False)


def read_inbox(member_id: str) -> str:
    """读取某个队友的 inbox 消息(不消费,仅查看)。"""
    if member_id not in TEAM_MEMBERS:
        return json.dumps({
            "error": f"未知团队成员: {member_id}",
            "available": list(TEAM_MEMBERS.keys()),
        }, ensure_ascii=False)

    inbox = TEAM_MEMBERS[member_id]["inbox"]
    return json.dumps({
        "member_id": member_id,
        "unread_count": len(inbox),
        "messages": inbox,
    }, ensure_ascii=False)


def list_team() -> str:
    """列出团队成员及其状态(inbox 消息数、唤醒次数)。"""
    return json.dumps({
        "members": [
            {
                "member_id": mid,
                "description": config["description"],
                "inbox_unread": len(config["inbox"]),
                "wake_count": config["wake_count"],
                "has_context": config["messages"] is not None,
            }
            for mid, config in TEAM_MEMBERS.items()
        ]
    }, ensure_ascii=False)


def print_team_panel():
    """打印团队面板(教学可视化)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  👥 Alex 团队面板(第 10 期新增)                      │")
    print("├" + "─" * 58 + "┤")

    members = list(TEAM_MEMBERS.keys())
    print(f"│  团队成员: {', '.join(members):<42s}│")
    print("│" + "─" * 58 + "│")

    for mid, config in TEAM_MEMBERS.items():
        inbox_n = len(config["inbox"])
        wakes = config["wake_count"]
        has_ctx = "有" if config["messages"] is not None else "无"
        line = f"  {mid:14s} inbox={inbox_n} 唤醒={wakes} 上下文={has_ctx}"
        print(f"│{line:<56}│")

    print("│" + "─" * 58 + "│")

    # 显示最近的 inbox 消息
    all_msgs = []
    for mid, config in TEAM_MEMBERS.items():
        for msg in config["inbox"]:
            all_msgs.append((mid, msg))
    if all_msgs:
        print(f"│  未读消息({len(all_msgs)} 条):{'':<34}│")
        for mid, msg in all_msgs[-3:]:
            short = f"{msg['from']}→{mid}: {msg['content'][:30]}"
            if len(short) > 50:
                short = short[:47] + "..."
            print(f"│    {short:<52}│")
    else:
        print("│  (空)inbox 里没有未读消息                              │")

    print("└" + "─" * 58 + "┘")
    print()
