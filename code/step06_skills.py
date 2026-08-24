#!/usr/bin/env python3
"""step06_skills.py — 第 06 期:Skills 按需加载

本期目标:
1. 解决第 05 期埋的伏笔:工具多了全塞 TOOLS 会占大量 token
2. 引入 skills/ 目录 + SKILL.md frontmatter,实现"两级加载":
   - 第一级(启动时):只加载技能索引(name + description),注入 system prompt
   - 第二级(按需):模型判断任务相关 → 调用 load_skill 工具 → SKILL.md 全文注入
3. 两个示例技能:weather(流程型) + git-cheatsheet(知识型,学员亲手写)

累积式:step06 = step05 + 技能索引 + load_skill 工具
(保留 step05 的工具循环全部逻辑,只新增技能层)

运行:
    python code/step06_skills.py
"""
import os
import json
import ast
import operator
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

load_dotenv()

# ============ 1. 初始化客户端(与第 01-05 期一致)============
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# 上下文长度上限(轮数,1 轮 = 1 user + 1 assistant)
MAX_ROUNDS = 10

# ============ 2. 加载 System Prompt(与第 04-05 期一致)============
SOUL_PATH = Path(__file__).parent.parent / "templates" / "SOUL.md"


def load_system_prompt() -> str:
    """读取 templates/SOUL.md 作为 system prompt(第 04 期引入)。"""
    if not SOUL_PATH.exists():
        raise FileNotFoundError(
            f"找不到人设文件: {SOUL_PATH}\n"
            f"请确认 templates/SOUL.md 已创建(第 04 期引入)"
        )
    return SOUL_PATH.read_text(encoding="utf-8")


# ============ 3. 技能层(本期核心新增)============
# 约定:每个技能是一个目录 skills/<name>/SKILL.md
# SKILL.md 头部是 YAML frontmatter(--- 包围),必须包含 name 和 description
# 约定:目录名与 frontmatter 的 name 保持一致(load_skill 按目录名查找)

SKILLS_DIR = Path(__file__).parent.parent / "skills"


def parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md 的 YAML frontmatter(--- 包围的头部)。

    只做最小实现:取 --- 和 --- 之间的 "key: value" 行。
    教学场景够用,不引入 pyyaml 依赖。
    """
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
    """扫描 skills/*/SKILL.md,返回技能索引(第一级:只取 name + description)。

    这就是"按需加载"的第一级:启动时只读每份 SKILL.md 的前几行,
    索引常驻上下文,全文等到要用时再加载。
    """
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
            continue  # 没有 frontmatter 的文件不算技能
        index.append({
            "name": meta["name"],
            "description": meta.get("description", ""),
            "path": str(skill_md),
            "size_chars": skill_md.stat().st_size,
        })
    return index


def build_skills_prompt(index: list[dict]) -> str:
    """把技能索引格式化为文本,追加到 system prompt 尾部。

    关键:这里只放 name + description(每个技能约 30-50 字),
    20 个技能的索引也就几百 token——这就是"索引常驻"的成本。
    """
    if not index:
        return ""
    lines = [
        "",
        "## 可用技能(Skills)",
        "以下技能默认未加载。如果当前任务与某个技能相关,"
        "先调用 load_skill 工具加载全文,再按技能内容回答:",
    ]
    for s in index:
        lines.append(f"- {s['name']}: {s['description']}")
    return "\n".join(lines)


# ============ 4. 工具定义(第 05 期 2 个 + 本期新增 1 个)============
# 用 JSON schema 描述工具,模型据此决定何时调用、传什么参数

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。当用户问'现在几点''今天日期''今天星期几'等问题时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "目标时区,如 Asia/Shanghai、US/Eastern。默认 Asia/Shanghai。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "进行四则运算。当用户要求计算数学表达式时调用,支持加减乘除和括号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式,如 '17 * 23'、'100 + 200'、'(5 + 3) * 2'",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        # ↓↓↓ 本期新增:技能加载工具(第二级加载的入口)↓↓↓
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "加载一个技能的完整内容。当当前任务与技能索引中某个技能的 "
                "description 相关时,先调用此工具拿到技能全文,再按技能内容回答。"
                "与天气相关用 weather,与 Git 命令相关用 git-cheatsheet。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能名,来自技能索引,如 weather、git-cheatsheet",
                    }
                },
                "required": ["name"],
            },
        },
    },
]


# ============ 5. 工具实现(第 05 期 2 个 + 本期新增 1 个)============

# 安全的数学运算:用 AST 解析表达式,只允许四则运算节点
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_eval(node):
    """递归计算 AST 节点,只允许数字和四则运算。"""
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
    """执行工具,返回结果字符串。

    模型决定调用哪个工具、传什么参数,实际执行在这里。
    本期新增 load_skill 分支:读取 skills/<name>/SKILL.md 全文返回。
    """
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

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


# ============ 6. 工具循环(与第 05 期一致,技能加载复用它)============

def chat_with_tools(messages: list[dict]) -> tuple[str, list[dict], int, int]:
    """带工具调用的对话:返回 (最终回答, 更新后的messages, prompt_tokens, completion_tokens)。

    技能加载天然复用第 05 期的工具循环:
      1. 用户问"Git 怎么撤销提交?"
      2. 模型看到 system prompt 里的技能索引,返回 tool_calls=[load_skill("git-cheatsheet")]
      3. 我们执行 load_skill,把 SKILL.md 全文作为 tool 结果喂回去
      4. 再次调用 API,模型按技能内容生成最终回答
    """
    total_prompt = 0
    total_completion = 0

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",  # auto = 模型自己决定要不要调工具
            max_tokens=1000,
        )
        msg = response.choices[0].message
        total_prompt += response.usage.prompt_tokens
        total_completion += response.usage.completion_tokens

        # 情况 A:模型决定调用工具
        if msg.tool_calls:
            messages.append({
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
                print(f"  [工具调用] {fn_name}({fn_args})")
                result = execute_tool(fn_name, fn_args)
                # load_skill 的结果较长,终端只显示前 80 字
                display = result if len(result) <= 80 else result[:80] + "..."
                print(f"  [工具结果] {display}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
            continue

        # 情况 B:模型返回文本回答(没有调用工具)
        messages.append({"role": "assistant", "content": msg.content})
        return msg.content, messages, total_prompt, total_completion


def trim_history(messages: list[dict]) -> list[dict]:
    """上下文长度限制:超过 MAX_ROUNDS 轮时截断(与第 04-05 期一致)。

    system prompt(含技能索引)永远保留,只截断 user/assistant/tool 对话。
    """
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
    print('第 06 期:Skills 按需加载 —— 给 Agent 配一个"技能库"')
    print("=" * 60)
    print(f"当前模型: {MODEL}")
    print(f"API 地址: {client.base_url}")
    print(f"上下文上限: {MAX_ROUNDS} 轮(超出自动截断最早)")
    print(f"已装工具: {', '.join(t['function']['name'] for t in TOOLS)}")

    # ============ 技能索引(第一级加载)============
    skills_index = load_skills_index()
    print(f"技能目录: {SKILLS_DIR}")
    print(f"技能索引: {len(skills_index)} 个(只加载了 name + description)")
    for s in skills_index:
        print(f"  - {s['name']}: {s['description'][:40]}...")

    # ============ 组装 system prompt:人设 + 技能索引 ============
    try:
        system_prompt = load_system_prompt()
        print(f"[人设] 已加载: {SOUL_PATH}")
    except FileNotFoundError as e:
        print(f"[出错] {e}")
        return
    skills_prompt = build_skills_prompt(skills_index)
    if skills_prompt:
        system_prompt = system_prompt + "\n" + skills_prompt
        print(f"[技能] 索引已注入 system prompt(全文未加载,按需再取)")

    print("=" * 60)

    # ============ 7. 自动演示:技能加载全过程 ============
    print('\n[自动演示] 问"Git 怎么撤销上次提交",看技能加载全过程:\n')

    demo_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Git 怎么撤销上次提交?保留修改的那种。"},
    ]
    try:
        answer, demo_messages, p, c = chat_with_tools(demo_messages)
        print(f"\n[最终回答] {answer}")
        print(f"[token] prompt={p}  completion={c}  total={p + c}")
        print(f"[历史] 技能加载结束后共 {len(demo_messages)} 条消息")
    except Exception as e:
        print(f"[出错] {e}")
        return

    print(
        "\n💡 看到了吗?system prompt 里只有技能索引(几十字),\n"
        "   模型判断「Git 问题」与 git-cheatsheet 相关 → 调 load_skill → 全文注入 → 按内容回答。\n"
        "   20 个技能常驻的也只是索引,这就是「按需加载」。\n"
    )

    # ============ 8. 交互式对话(带人设 + 记忆 + 工具 + 技能)============
    print("=" * 60)
    print("现在进入自由对话(Alex 技术助理,带记忆 + 人设 + 工具 + 技能)")
    print("输入 quit 退出 / reset 清空历史 / history 查看历史 / soul 查看人设")
    print("     tools 查看工具 / skills 查看技能索引")
    print("←/→ 移动光标,↑/↓ 翻历史,Ctrl-C 作废当前行重输")
    print("=" * 60)

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
            print("再见!")
            break
        if user_input.lower() == "reset":
            messages = [{"role": "system", "content": system_prompt}]
            total_prompt = 0
            total_completion = 0
            print("[提示] 对话已清空,人设和技能索引保留,Alex 还在。")
            continue
        if user_input.lower() == "history":
            print(f"[历史] 共 {len(messages)} 条消息:")
            for idx, m in enumerate(messages):
                role = m["role"]
                content = m.get("content", "")
                if m.get("tool_calls") and not content:
                    calls = ", ".join(
                        tc["function"]["name"] for tc in m["tool_calls"]
                    )
                    content = f"[tool_calls: {calls}]"
                elif not content:
                    content = "(无文本)"
                content = content[:60] + ("..." if len(content) > 60 else "")
                print(f"  {idx:2d}. [{role:9s}] {content}")
            continue
        if user_input.lower() == "soul":
            print(f"[人设] {SOUL_PATH}(尾部含技能索引):\n")
            print(system_prompt)
            continue
        if user_input.lower() == "tools":
            print(f"[工具] 共 {len(TOOLS)} 个:")
            for t in TOOLS:
                fn = t["function"]
                params = fn.get("parameters", {}).get("properties", {})
                param_str = ", ".join(f"{k}: {v['description']}" for k, v in params.items())
                print(f"  - {fn['name']}({param_str})")
                print(f"    {fn['description']}")
            continue
        if user_input.lower() == "skills":
            print(f"[技能] 索引共 {len(skills_index)} 个(全文未加载):")
            for s in skills_index:
                print(f"  - {s['name']}: {s['description']}")
                print(f"    路径: {s['path']}({s['size_chars']} 字符,用到才加载)")
            continue
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})
        messages = trim_history(messages)
        try:
            answer, messages, p, c = chat_with_tools(messages)
            total_prompt += p
            total_completion += c
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
