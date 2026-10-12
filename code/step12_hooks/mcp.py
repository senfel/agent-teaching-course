"""mcp.py — MCP 层(第 11 期新增)

Model Context Protocol:让 Agent 接上"外部工具服务器"。
工具不再写死在 Agent 代码里,而是跑在独立的服务器进程中,
双方通过 stdio(每行一条 JSON-RPC 2.0 消息)通话。

[第 11 期] 本模块新增:
  - MCP_SERVERS      服务器注册表(server_id → 启动命令 + 运行时状态)
  - _McpConnection   stdio 连接类(起子进程 + 握手 + 请求/响应配对)
  - 4 个工具实现     list_mcp_servers / mcp_connect / mcp_list_tools / mcp_call_tool
  - shutdown_mcp     退出前收掉所有子进程(不留孤儿进程)
  - print_mcp_panel  MCP 面板(教学可视化)

stdio transport 一图流:
  ┌──────────── Agent 进程(step11)────────────┐
  │  mcp_call_tool("repo", "get_repository_info",│
  │                {"owner": "deepseek-ai", ...})│
  └──────┬─────────────────────────────────────┘
         │ stdin  ← 每行一条 JSON-RPC 请求
         ▼
  ┌──────────── MCP 服务器进程(repo_server.py)──┐
  │  {"jsonrpc":"2.0","id":3,"method":"tools/call", ...}
  │  → 查数据 → 组织结果 → 写回 stdout          │
  └──────┬─────────────────────────────────────┘
         │ stdout ← 每行一条 JSON-RPC 响应
         ▼
  Agent 收到 {"id":3,"result":{"content":[{"type":"text",...}]}}

铁律:服务器的 stdout 是"协议专线",日志必须打 stderr。
"""
import json
import subprocess
import sys

from .config import REPO_SERVER_PATH

MCP_PROTOCOL_VERSION = "2024-11-05"

# MCP 服务器注册表:server_id → 配置(连接命令 + 运行时状态)
# 预配置一个教学模拟的"GitHub 风格仓库服务器";
# 生产中把 command 换成真实 MCP server(如 GitHub 官方 server),协议完全一样
MCP_SERVERS: dict[str, dict] = {
    "repo": {
        "description": (
            "GitHub 风格仓库信息服务器(教学模拟)。"
            "提供 search_repositories / get_repository_info / list_issues 三个工具。"
        ),
        "command": [sys.executable, str(REPO_SERVER_PATH)],
        "conn": None,   # 连接对象(_McpConnection),None = 未连接
        "tools": [],    # initialize 握手后缓存的工具清单
    },
}


class _McpConnection:
    """MCP stdio 连接:子进程 + JSON-RPC 2.0(每行一条 JSON 消息)。

    把子进程的 stdin/stdout 当"电话线":
      Agent → server.stdin   发 JSON 请求
      Agent ← server.stdout  收 JSON 响应
    注意:server 的日志必须打到 stderr——stdout 被协议独占,混进日志会污染协议流。
    """

    def __init__(self, server_id: str, command: list[str]):
        self.server_id = server_id
        self.req_id = 0  # JSON-RPC 请求计数,用来配对"请求/响应"
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # 教学演示时不显示 server 日志;想看可改为 None
            text=True,
            bufsize=1,
        )
        self.server_info: dict = {}
        self.tools: list[dict] = []
        self._initialize()  # 启动后立刻握手

    # ---- 协议底层:发一行 / 收一行 ----
    def _send(self, obj: dict):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue  # 跳过无法解析的行,继续等真正的 JSON-RPC 响应
        raise ConnectionError(f"MCP 服务器 {self.server_id} 已断开(stdout 关闭)")

    def _request(self, method: str, params: dict | None = None) -> dict:
        self.req_id += 1
        msg = {"jsonrpc": "2.0", "id": self.req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        # 服务器可能穿插发别的消息,只认 id 配对的那条响应
        while True:
            resp = self._recv()
            if resp.get("id") == self.req_id:
                if "error" in resp:
                    raise RuntimeError(f"MCP {method} 出错: {resp['error']}")
                return resp.get("result", {})

    # ---- 协议三步:握手 → 拿工具 → 调工具 ----
    def _initialize(self):
        result = self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "alex-agent-step11", "version": "1.0.0"},
        })
        self.server_info = result
        # 握手完成必须回一个 initialized 通知(MCP 规范要求)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # 顺手把工具清单缓存下来(mcp_list_tools 直接读缓存)
        self.tools = self._request("tools/list").get("tools", [])

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        result = self._request("tools/call", {"name": tool_name, "arguments": arguments})
        # MCP 工具结果统一装在 content 数组里,教学场景只取文本部分
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        if result.get("isError"):
            return json.dumps({"error": "\n".join(texts) or "MCP 工具执行失败"}, ensure_ascii=False)
        return "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


# ---- MCP 工具实现(暴露给 LLM 的 4 个) ----
def mcp_connect(server_id: str) -> str:
    config = MCP_SERVERS.get(server_id)
    if config is None:
        return json.dumps({
            "error": f"MCP 服务器不存在: {server_id}",
            "available": ", ".join(MCP_SERVERS.keys()),
        }, ensure_ascii=False)
    if config["conn"] is not None:
        return json.dumps({
            "server": server_id, "status": "already_connected",
            "note": "连接已存在,无需重复连接",
            "tools": [t["name"] for t in config["tools"]],
        }, ensure_ascii=False)
    conn = _McpConnection(server_id, config["command"])
    config["conn"] = conn
    config["tools"] = conn.tools
    return json.dumps({
        "server": server_id,
        "status": "connected",
        "server_info": conn.server_info.get("serverInfo", {}),
        "tools": [{"name": t["name"], "description": t.get("description", "")}
                  for t in conn.tools],
    }, ensure_ascii=False)


def mcp_list_tools(server_id: str) -> str:
    config = MCP_SERVERS.get(server_id)
    if config is None:
        return json.dumps({"error": f"MCP 服务器不存在: {server_id}"}, ensure_ascii=False)
    if config["conn"] is None:
        return json.dumps({
            "error": f"尚未连接 {server_id},请先调用 mcp_connect",
        }, ensure_ascii=False)
    return json.dumps({
        "server": server_id,
        "tools": [
            {"name": t["name"], "description": t.get("description", ""),
             "inputSchema": t.get("inputSchema", {})}
            for t in config["tools"]
        ],
    }, ensure_ascii=False)


def mcp_call_tool(server_id: str, tool_name: str, arguments: dict) -> str:
    config = MCP_SERVERS.get(server_id)
    if config is None:
        return json.dumps({"error": f"MCP 服务器不存在: {server_id}"}, ensure_ascii=False)
    conn = config["conn"]
    if conn is None:
        # 未连接就先自动连上(MCP 的"即插即用"体验)
        conn = _McpConnection(server_id, config["command"])
        config["conn"] = conn
        config["tools"] = conn.tools
    known = [t["name"] for t in config["tools"]]
    if tool_name not in known:
        return json.dumps({
            "error": f"服务器 {server_id} 上没有工具 {tool_name}",
            "available": known,
        }, ensure_ascii=False)
    return conn.call_tool(tool_name, arguments)


def list_mcp_servers() -> str:
    servers = []
    for sid, cfg in MCP_SERVERS.items():
        servers.append({
            "server_id": sid,
            "description": cfg["description"],
            "connected": cfg["conn"] is not None,
            "tool_count": len(cfg["tools"]),
        })
    return json.dumps({"mcp_servers": servers}, ensure_ascii=False)


def shutdown_mcp():
    """程序退出前,把所有 MCP 子进程收掉(不留孤儿进程)。"""
    for sid, cfg in MCP_SERVERS.items():
        if cfg["conn"] is not None:
            cfg["conn"].stop()
            cfg["conn"] = None


def print_mcp_panel():
    """打印 MCP 面板(教学可视化)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  🔌 Alex MCP 面板(第 11 期新增)                          │")
    print("├" + "─" * 58 + "┤")
    print(f"│  MCP 服务器注册表: {', '.join(MCP_SERVERS.keys())}")
    print("│" + "─" * 58 + "│")
    for sid, cfg in MCP_SERVERS.items():
        connected = cfg["conn"] is not None
        status = "🟢 已连接" if connected else "⚪ 未连接"
        print(f"│  {sid}  {status}  工具数={len(cfg['tools'])}")
        print(f"│    {cfg['description']}")
        if connected and cfg["tools"]:
            print(f"│    工具: {', '.join(t['name'] for t in cfg['tools'])}")
    print("└" + "─" * 58 + "┘")
