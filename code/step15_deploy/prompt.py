"""prompt.py — System Prompt 人设层 + Skills 技能层 + 组装器

[第 04 期] 人设:从 templates/SOUL.md 加载 system prompt
[第 06 期] 技能:扫描 skills/ 目录,构建技能索引提示词(按需 load_skill)
[第 15 期] 组装:build_system_prompt = 人设 + 技能 + 画像 + TodoList + 子代理
           + Team + MCP + Hooks + 多模型路由 + 目标驱动(CLI 与 Web 共用)
"""
from .config import SOUL_PATH, USER_PATH, SKILLS_DIR, TODO_GUIDE_PATH
from .model_router import TIER_DEFAULT

# ---- Alex 回答高亮(第 14 期):浅蓝底黑字,控制台一眼认出"这是它说的话" ----
_ANSI_ALEX_BG = "\033[30;48;5;153m"   # 256 色 153 号浅蓝底 + 黑字
_ANSI_RESET = "\033[0m"


def print_alex(text: str) -> None:
    """Alex 的回答统一浅底高亮打印 —— 宿主日志无底色,Alex 话有底色。"""
    print(f"{_ANSI_ALEX_BG}{text}{_ANSI_RESET}")


def load_system_prompt() -> str:
    """读取 SOUL.md(第 04 期)作为人设核心。"""
    if not SOUL_PATH.exists():
        raise FileNotFoundError(f"找不到人设文件: {SOUL_PATH}")
    return SOUL_PATH.read_text(encoding="utf-8")


def load_user_profile() -> str:
    """读取 templates/USER.md 作为长期记忆 / 用户画像(第 07 期)。"""
    if not USER_PATH.exists():
        return ""
    return USER_PATH.read_text(encoding="utf-8")


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}
    meta = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def load_skills_index() -> list[dict]:
    if not SKILLS_DIR.exists():
        return []
    index = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        meta = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        if not meta.get("name"):
            continue
        index.append({
            "name": meta["name"],
            "description": meta.get("description", ""),
            "path": str(skill_dir),
            "size_chars": skill_md.stat().st_size,
        })
    return index


def build_skills_prompt(index: list[dict]) -> str:
    if not index:
        return ""
    lines = [
        "",
        "## 可用技能(Skills)",
        "以下技能默认未加载。如果当前任务与某个技能相关,",
        "先调用 load_skill 工具加载全文,再按技能内容回答:",
    ]
    for s in index:
        lines.append(f"- {s['name']}: {s['description']}")
    return "\n".join(lines)


def build_system_prompt() -> str:
    """第 15 期:把"谁说话 + 会什么 + 长什么样 + 怎么干活 + 边界在哪"拼成
    一条完整 system prompt。

    前 14 期这段组装逻辑内联在 main() 里(只 CLI 一个入口用)。
    第 15 期要同时服务 CLI 与 Web 两个入口,于是抽到 prompt.py:
    build_system_prompt() 成为"system prompt 的唯一来源" ——
    CLI 改一行、Web 同步生效,不再有两份拼装代码跑偏。

    组装顺序(每段对应一期新增的能力):
      人设(04)+ 技能索引(06)+ 用户画像(07)+ TodoList 指南(08)
      + 子代理指南(09)+ Team 指南(10)+ MCP 指南(11)
      + Hooks 指南(12)+ 多模型路由指南(13)+ 目标驱动指南(14)
    """
    system_prompt = load_system_prompt()

    skills_prompt = build_skills_prompt(load_skills_index())
    if skills_prompt:
        system_prompt += "\n" + skills_prompt

    user_profile = load_user_profile()
    if user_profile:
        system_prompt += "\n\n# 用户画像(长期记忆)\n\n" + user_profile

    if TODO_GUIDE_PATH.exists():
        system_prompt += "\n" + TODO_GUIDE_PATH.read_text(encoding="utf-8")

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

    return system_prompt
