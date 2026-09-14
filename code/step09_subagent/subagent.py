"""subagent.py — Subagent 子代理层(第 09 期核心新增)

[第 09 期] 三个核心概念:
1. 独立上下文:子代理有自己的 messages,不复用主 Agent 的对话历史
2. 工具白名单:子代理只能用 allowed_tools 中的工具(LLM 看不到未授权工具 = 物理隔离)
3. 并发派遣:ThreadPoolExecutor 同时跑多个子代理,总耗时 ≈ max(单个耗时)
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import client, MODEL
from .prompt import load_system_prompt, load_skills_index, build_skills_prompt
from .tools import TOOLS, execute_tool


# ============ 子代理类型注册表 ============
SUBAGENT_TYPES = {
    "code_reviewer": {
        "description": "代码审查员。审查代码质量,指出 bug、性能、安全问题,给出改进建议。",
        "allowed_tools": ["get_current_time", "calculate", "load_skill"],
        "extra_prompt": (
            "\n\n## 子代理角色:代码审查员\n"
            "你是代码审查专家。你的职责:\n"
            "1. 审查代码逻辑正确性\n"
            "2. 指出潜在 bug 和边界情况\n"
            "3. 检查性能问题\n"
            "4. 给出具体的改进建议\n\n"
            "你不能创建任务(todo_create)、不能修改用户画像、不能派遣其他子代理。\n"
        ),
    },
    "doc_writer": {
        "description": "文档整理员。整理文档结构、优化措辞、生成摘要、规范格式。",
        "allowed_tools": ["get_current_time", "calculate", "load_skill"],
        "extra_prompt": (
            "\n\n## 子代理角色:文档整理员\n"
            "你是技术文档专家。你的职责:\n"
            "1. 整理文档结构(标题层级、段落划分)\n"
            "2. 优化措辞(技术术语准确、表达简洁)\n"
            "3. 生成摘要(提炼核心要点)\n"
            "4. 规范格式(markdown 格式正确)\n\n"
            "你不能创建任务、不能修改用户画像、不能派遣其他子代理。\n"
        ),
    },
}

# 子代理运行时记录(可视化用)
_subagent_runs: list[dict] = []


def _run_subagent(agent_type: str, task: str) -> tuple[str, int, int]:
    """执行单个子代理(独立上下文 + 工具白名单)。

    三个核心概念:
    1. 独立上下文:全新 messages,不复用主 Agent 对话历史
    2. 工具白名单:只保留 allowed_tools 中的工具定义(LLM 看不到未授权工具)
    3. 自己的工具循环:复用第 05 期的 while True 循环,但用受限工具集

    返回:(结果文本, prompt_tokens, completion_tokens)
    """
    config = SUBAGENT_TYPES[agent_type]

    # ① 独立上下文:全新 messages,不复用主 Agent 对话历史
    sub_system = load_system_prompt() + config["extra_prompt"]
    skills_index = load_skills_index()
    skills_prompt = build_skills_prompt(skills_index)
    if skills_prompt:
        sub_system += "\n" + skills_prompt

    sub_messages = [
        {"role": "system", "content": sub_system},
        {"role": "user", "content": task},
    ]

    # ② 工具白名单:只保留 allowed_tools 中的工具
    allowed = set(config["allowed_tools"])
    sub_tools = [t for t in TOOLS if t["function"]["name"] in allowed]

    # ③ 子代理工具循环(复用主循环逻辑,但用受限工具集和独立 messages)
    total_prompt = 0
    total_completion = 0

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=sub_messages,
            tools=sub_tools,
            tool_choice="auto",
            max_tokens=2048,
        )
        msg = response.choices[0].message
        total_prompt += response.usage.prompt_tokens
        total_completion += response.usage.completion_tokens

        if msg.tool_calls:
            sub_messages.append({
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
                print(f"    [子代理·{agent_type}] 工具调用: {fn_name}({fn_args})")
                result = execute_tool(fn_name, fn_args)
                display = result if len(result) <= 80 else result[:80] + "..."
                print(f"    [子代理·{agent_type}] 结果: {display}")
                sub_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
            continue

        sub_messages.append({"role": "assistant", "content": msg.content})
        return msg.content, total_prompt, total_completion


def dispatch_subagent(agent_type: str, task: str) -> str:
    """派遣单个子代理(同步,阻塞直到完成)。

    适用场景:只需要一个专家完成特定任务时使用。
    """
    if agent_type not in SUBAGENT_TYPES:
        return json.dumps({
            "error": f"未知子代理类型: {agent_type}",
            "available": list(SUBAGENT_TYPES.keys()),
        }, ensure_ascii=False)

    print(f"  [子代理] 派遣 {agent_type}...")
    start = time.time()
    result, p, c = _run_subagent(agent_type, task)
    elapsed = time.time() - start

    _subagent_runs.append({
        "type": agent_type,
        "task": task[:200],
        "result": result,
        "prompt_tokens": p,
        "completion_tokens": c,
        "elapsed": round(elapsed, 2),
        "parallel": False,
    })

    return json.dumps({
        "agent_type": agent_type,
        "result": result,
        "prompt_tokens": p,
        "completion_tokens": c,
        "elapsed_seconds": round(elapsed, 2),
    }, ensure_ascii=False)


def dispatch_subagents_parallel(tasks: list[dict]) -> str:
    """并发派遣多个子代理。

    tasks: [{"agent_type": "code_reviewer", "task": "审查这段代码..."}, ...]

    使用 ThreadPoolExecutor 实现真正的并发执行:
    - 每个子代理在自己的线程中运行
    - 总耗时 ≈ max(各子代理耗时),不是 sum(各子代理耗时)
    - 对比串行:串行总耗时 = sum,并发总耗时 = max
    """
    if not tasks or not isinstance(tasks, list):
        return json.dumps({"error": "tasks 必须是非空列表"}, ensure_ascii=False)

    # 验证所有类型
    for t in tasks:
        agent_type = t.get("agent_type", "")
        if agent_type not in SUBAGENT_TYPES:
            return json.dumps({
                "error": f"未知子代理类型: {agent_type}",
                "available": list(SUBAGENT_TYPES.keys()),
            }, ensure_ascii=False)

    n = len(tasks)
    print(f"  [子代理] 并发派遣 {n} 个子代理: {', '.join(t['agent_type'] for t in tasks)}")
    start_time = time.time()

    results = [None] * n  # 预分配,保持原始顺序

    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = {}
        for i, t in enumerate(tasks):
            future = executor.submit(_run_subagent, t["agent_type"], t["task"])
            futures[future] = i

        for future in as_completed(futures):
            idx = futures[future]
            try:
                result, p, c = future.result()
                _subagent_runs.append({
                    "type": tasks[idx]["agent_type"],
                    "task": tasks[idx]["task"][:200],
                    "result": result,
                    "prompt_tokens": p,
                    "completion_tokens": c,
                    "elapsed": None,  # 填在汇总里
                    "parallel": True,
                })
                results[idx] = {
                    "index": idx,
                    "agent_type": tasks[idx]["agent_type"],
                    "result": result,
                    "prompt_tokens": p,
                    "completion_tokens": c,
                }
            except Exception as e:
                results[idx] = {
                    "index": idx,
                    "agent_type": tasks[idx]["agent_type"],
                    "error": str(e),
                }

    elapsed = time.time() - start_time

    # 填充每个 run 的 elapsed(并发场景下统一用总耗时)
    for run in _subagent_runs[-n:]:
        if run.get("parallel"):
            run["elapsed"] = round(elapsed, 2)

    return json.dumps({
        "dispatched": n,
        "elapsed_seconds": round(elapsed, 2),
        "results": results,
    }, ensure_ascii=False)


def list_subagents() -> str:
    """列出可用的子代理类型及其工具白名单。"""
    return json.dumps({
        "available": [
            {
                "type": name,
                "description": config["description"],
                "allowed_tools": config["allowed_tools"],
            }
            for name, config in SUBAGENT_TYPES.items()
        ]
    }, ensure_ascii=False)


def print_subagent_panel():
    """打印子代理面板(教学可视化)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  🔀 Alex 子代理面板(第 09 期新增)                     │")
    print("├" + "─" * 58 + "┤")

    # 可用类型
    types = list(SUBAGENT_TYPES.keys())
    print(f"│  可用类型: {', '.join(types):<40s}│")
    print("│" + "─" * 58 + "│")

    if not _subagent_runs:
        print("│  (空)还没有派遣过子代理                               │")
    else:
        for run in _subagent_runs[-5:]:  # 最近 5 个
            short_task = run["task"][:30] + "..." if len(run["task"]) > 30 else run["task"]
            mode = "并发" if run.get("parallel") else "单独"
            line = f"  {run['type']:14s} [{mode}] {short_task}"
            print(f"│{line:<56}│")
            tokens = run["prompt_tokens"] + run["completion_tokens"]
            elapsed = run.get("elapsed", 0)
            line2 = f"    token={tokens}  耗时={elapsed}s  结果={len(run['result'])}字符"
            print(f"│{line2:<56}│")

    print("└" + "─" * 58 + "┘")
    print()
