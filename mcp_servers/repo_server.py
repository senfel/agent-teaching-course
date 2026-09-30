#!/usr/bin/env python3
"""repo_server.py — 第 11 期教学用的 MCP 工具服务器(GitHub 风格,模拟数据)

这是一个独立进程,实现 MCP 协议的服务器端:
- transport:stdio(从 stdin 收 JSON-RPC 2.0 请求,往 stdout 写 JSON-RPC 响应)
- 方法:initialize / notifications/initialized / tools/list / tools/call
- 工具:search_repositories / get_repository_info / list_issues

关键铁律(和真实 MCP server 一样):
    stdout 是"协议专线",只能写 JSON-RPC 消息;
    日志/调试信息一律打到 stderr,混进 stdout 会污染协议流,客户端直接解析失败。

生产对照:把本文件换成 GitHub 官方 MCP server(npx 或 docker 启动),
客户端(step11_mcp.py 的 _McpConnection)一行代码都不用改——这就是协议的价值。

运行(独立运行也可以手动体验):
    python mcp_servers/repo_server.py
    然后逐行输入 JSON-RPC 消息,Ctrl-D 结束
"""
import json
import sys

SERVER_INFO = {
    "name": "repo-server",
    "version": "1.0.0",
}
PROTOCOL_VERSION = "2024-11-05"

# ── 模拟数据(教学用;真实 GitHub server 会调 API) ──────────────────
FAKE_REPOS = {
    "deepseek-ai/DeepSeek-V3": {
        "owner": "deepseek-ai",
        "repo": "DeepSeek-V3",
        "description": "DeepSeek-V3 是一个拥有 671B 参数、37B 激活的 MoE 大模型,性能对齐开源顶尖。",
        "stars": 85000,
        "forks": 12000,
        "language": "Python",
        "open_issues": 42,
        "url": "https://github.com/deepseek-ai/DeepSeek-V3",
    },
    "deepseek-ai/DeepSeek-R1": {
        "owner": "deepseek-ai",
        "repo": "DeepSeek-R1",
        "description": "DeepSeek-R1 是一个通过大规模强化学习训练的推理模型,擅长数学、代码和逻辑。",
        "stars": 92000,
        "forks": 15000,
        "language": "Python",
        "open_issues": 58,
        "url": "https://github.com/deepseek-ai/DeepSeek-R1",
    },
    "deepseek-ai/DeepSeek-Coder": {
        "owner": "deepseek-ai",
        "repo": "DeepSeek-Coder",
        "description": "DeepSeek-Coder 是一个从头训练的代码模型,支持 128K 上下文仓库级代码补全。",
        "stars": 21000,
        "forks": 2800,
        "language": "Python",
        "open_issues": 23,
        "url": "https://github.com/deepseek-ai/DeepSeek-Coder",
    },
    "openai/openai-cookbook": {
        "owner": "openai",
        "repo": "openai-cookbook",
        "description": "OpenAI API 使用示例和指南大全。",
        "stars": 32000,
        "forks": 4800,
        "language": "Jupyter Notebook",
        "open_issues": 66,
        "url": "https://github.com/openai/openai-cookbook",
    },
}

FAKE_ISSUES = {
    "deepseek-ai/DeepSeek-V3": [
        {"number": 156, "title": "V3 的 MoE 路由机制文档不够细", "author": "learner-a", "state": "open"},
        {"number": 149, "title": "请求提供一个最小的推理示例", "author": "dev-b", "state": "open"},
        {"number": 138, "title": "Tokenizer 特殊 token 汇总表", "author": "nlp-c", "state": "open"},
    ],
    "deepseek-ai/DeepSeek-R1": [
        {"number": 201, "title": "R1 的长思维链偶尔会中断", "author": "rl-researcher", "state": "open"},
        {"number": 188, "title": "蒸馏小模型的评估报告什么时候出?", "author": "eval-fan", "state": "open"},
    ],
}

# ── 工具清单(名称 + 描述 + JSON schema,和内置工具的 schema 同构) ──
TOOLS = [
    {
        "name": "search_repositories",
        "description": "按关键词搜索 GitHub 仓库,返回仓库名、简介、star 数,按 star 数从高到低排序。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词,如 'deepseek'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_repository_info",
        "description": "查询单个仓库的详细信息(owner/repo/star/fork/语言/开放 issue 数等)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "组织或用户名,如 'deepseek-ai'"},
                "repo": {"type": "string", "description": "仓库名,如 'DeepSeek-V3'"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "list_issues",
        "description": "列出某个仓库的开放 issue(编号、标题、作者)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "组织或用户名,如 'deepseek-ai'"},
                "repo": {"type": "string", "description": "仓库名,如 'DeepSeek-R1'"},
            },
            "required": ["owner", "repo"],
        },
    },
]


def log(msg: str):
    """服务器日志 → stderr(严禁 print 到 stdout!)"""
    print(f"[repo-server] {msg}", file=sys.stderr, flush=True)


# ── 工具实现 ────────────────────────────────────────────────────────
def tool_search_repositories(query: str) -> str:
    q = query.lower()
    hits = []
    for full_name, info in FAKE_REPOS.items():
        if q in full_name.lower() or q in info["description"].lower():
            hits.append({
                "full_name": full_name,
                "description": info["description"],
                "stars": info["stars"],
            })
    hits.sort(key=lambda r: r["stars"], reverse=True)
    if not hits:
        return f"没有找到与 '{query}' 相关的仓库。"
    lines = [f"共找到 {len(hits)} 个与 '{query}' 相关的仓库(按 star 排序):"]
    for r in hits:
        lines.append(f"- {r['full_name']} ⭐{r['stars']}: {r['description']}")
    return "\n".join(lines)


def tool_get_repository_info(owner: str, repo: str) -> str:
    key = f"{owner}/{repo}"
    info = FAKE_REPOS.get(key)
    if info is None:
        return f"仓库不存在: {key}(教学模拟数据里只有 deepseek-ai 和 openai 的部分仓库)"
    return (
        f"仓库: {key}\n"
        f"简介: {info['description']}\n"
        f"Star: {info['stars']}  Fork: {info['forks']}  开放 issue: {info['open_issues']}\n"
        f"主语言: {info['language']}\n"
        f"地址: {info['url']}"
    )


def tool_list_issues(owner: str, repo: str) -> str:
    key = f"{owner}/{repo}"
    if key not in FAKE_REPOS:
        return f"仓库不存在: {key}(教学模拟数据里只有 deepseek-ai 和 openai 的部分仓库)"
    issues = FAKE_ISSUES.get(key, [])
    if not issues:
        return f"{key} 目前没有模拟的开放 issue。"
    lines = [f"{key} 的开放 issue({len(issues)} 个):"]
    for i in issues:
        lines.append(f"- #{i['number']} [{i['state']}] {i['title']}(by {i['author']})")
    return "\n".join(lines)


def call_tool(name: str, arguments: dict) -> str:
    """统一工具分发。找不到工具 → 抛 MCP 工具错误(isError=true)。"""
    if name == "search_repositories":
        return tool_search_repositories(arguments.get("query", ""))
    if name == "get_repository_info":
        return tool_get_repository_info(arguments.get("owner", ""), arguments.get("repo", ""))
    if name == "list_issues":
        return tool_list_issues(arguments.get("owner", ""), arguments.get("repo", ""))
    raise KeyError(f"未知工具: {name}")


# ── MCP 协议主循环(stdio) ──────────────────────────────────────────
def send(obj: dict):
    """协议响应 → stdout(每行一条 JSON)。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def send_result(req_id, result: dict):
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def send_error(req_id, code: int, message: str):
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def main():
    log("启动,等待 MCP 客户端(stdio)...")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"收到无法解析的行,忽略: {line[:80]}")
            continue

        method = msg.get("method", "")
        req_id = msg.get("id")

        # 通知(无 id,不回响应)
        if req_id is None:
            if method == "notifications/initialized":
                log("收到 initialized 通知,握手完成,进入服务状态")
            continue

        # 请求(有 id,必须回响应)
        if method == "initialize":
            log(f"握手请求: client={msg.get('params', {}).get('clientInfo', {})}")
            send_result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        elif method == "tools/list":
            log("返回工具清单(3 个)")
            send_result(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params", {})
            tname = params.get("name", "")
            targs = params.get("arguments", {})
            log(f"调用工具: {tname}({targs})")
            try:
                text = call_tool(tname, targs)
                send_result(req_id, {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                })
            except KeyError as e:
                # 工具不存在:按 MCP 规范,结果级错误用 isError=true(不是协议错误)
                send_result(req_id, {
                    "content": [{"type": "text", "text": str(e)}],
                    "isError": True,
                })
        elif method == "ping":
            send_result(req_id, {})
        else:
            log(f"未知方法: {method}")
            send_error(req_id, -32601, f"Method not found: {method}")
    log("stdin 关闭,退出")


if __name__ == "__main__":
    main()
