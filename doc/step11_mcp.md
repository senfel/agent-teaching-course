# 第 11 期:MCP 协议(外接工具服务器)

## 问题

第 05 期到现在,Alex 已经有 16 个工具了:看时间、算数、加载技能、记忆、TodoList、派遣子代理、组建团队……但仔细看 `execute_tool`,会发现一个尴尬的事实:**所有工具都写死在 Agent 的源码里**。

你想加一个"查 GitHub 仓库"的能力,流程是:

1. 写一个 `get_repository_info()` 函数,塞进 stepXX.py
2. 写一个 JSON schema,注册进 `TOOLS` 列表
3. 在 `execute_tool` 里加一个 `if` 分支
4. 重启 Agent

痛点清单:

- **工具越多代码越臃肿**——16 个工具 16 个 if,再接 10 个能力,execute_tool 会变成几百行
- **别人的工具没法直接复用**——同事写好的"查数据库""查文档"工具,你得把他的代码抄进来
- **换供应商 = 重写**——今天用 GitHub API,明天换成 GitLab,工具函数推倒重写
- **没法动态上下线**——工具加进代码就常驻,不用也占着

本质问题:工具和 Agent **耦合在同一个进程里**。缺一个标准协议,让工具可以跑在**外部服务器**上,Agent "连上就能用"。

## 解决方案

### 1. 三个核心概念

| 概念 | 含义 | 解决什么问题 |
|---|---|---|
| MCP(Model Context Protocol) | 连接"外部工具服务器"的开放协议 | 别人写好的工具,Agent 连上就能用,不用抄代码 |
| stdio transport | MCP 的第一种传输方式:把子进程的 stdin/stdout 当"电话线" | Agent 和服务器在两台"话机"上,靠 JSON 消息通话 |
| JSON-RPC 2.0 | MCP 的消息格式,每行一条 JSON,带 id 配对请求/响应 | 双方都按规范收发,谁写的服务器都能对话 |

```
┌─────────── Agent 进程(step11_mcp.py)───────────┐
│                                                  │
│  mcp_call_tool("repo", "get_repository_info",   │
│        {"owner": "deepseek-ai", "repo": "..."})  │
│                                                  │
└──────┬───────────────────────────────────────────┘
       │ stdin  → 每行一条 JSON-RPC 请求
       ▼
┌────── MCP 服务器进程(repo_server.py)────────────┐
│  收到 {"jsonrpc":"2.0","id":3,                  │
│         "method":"tools/call", ...}              │
│  → 查数据 → 组织结果 → 写回 stdout               │
└──────┬───────────────────────────────────────────┘
       │ stdout ← 每行一条 JSON-RPC 响应
       ▼
  Agent 收到 {"id":3,"result":{"content":[...]}}
```

### 2. 四个新工具

```python
# 查看有哪些 MCP 服务器可连接(名称、描述、连接状态)
list_mcp_servers()
# → {mcp_servers: [{server_id: "repo", description: "...", connected: false, tool_count: 3}]}

# 连接 MCP 服务器(启动子进程 + initialize 握手 + 拉取工具清单)
mcp_connect(server_id="repo")
# → {server: "repo", status: "connected", server_info: {...}, tools: [{name, description}, ...]}

# 列出服务器提供的工具(名称 + 描述 + 参数 schema)
mcp_list_tools(server_id="repo")
# → {server: "repo", tools: [{name: "search_repositories", inputSchema: {...}}, ...]}

# 调用服务器上的工具(工具名 + 参数 JSON)
mcp_call_tool(server_id="repo", tool_name="get_repository_info",
              arguments={"owner": "deepseek-ai", "repo": "DeepSeek-R1"})
# → 工具的文本结果(仓库详情)
```

这四个工具和第 05-10 期的工具**完全平等**——都走同一套工具循环。注意一个漂亮的对比:第 05 期加工具 = 加函数;本期加工具 = 连服务器。**Agent 的能力边界第一次跑到了自己的进程之外。**

### 3. MCP 协议三步:握手 → 拿工具 → 调工具

```python
class _McpConnection:
    def _initialize(self):
        # ① 握手:告诉服务器"我是谁、我支持什么协议版本"
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "alex-agent-step11", "version": "1.0.0"},
        })
        self.server_info = result
        # ② 握手完成必须回一个 initialized 通知(MCP 规范要求)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # ③ 顺手把工具清单缓存下来
        self.tools = self._request("tools/list").get("tools", [])

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        # ④ 调工具:tools/call + 工具名 + 参数
        result = self._request("tools/call", {"name": tool_name, "arguments": arguments})
        texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
        ...
```

消息格式长这样(每行一条 JSON):

```
→ {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {...}}
← {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "...", "serverInfo": {...}}}
→ {"jsonrpc": "2.0", "method": "notifications/initialized"}          ← 通知,不用回
→ {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
← {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{name, description, inputSchema}, ...]}}
→ {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
   "params": {"name": "search_repositories", "arguments": {"query": "deepseek"}}}
← {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "..."}]}}
```

关键细节:

- **id 配对**:请求带 `id`,响应回同一个 `id`。服务器可能穿插发别的消息,客户端只认配对的那条
- **通知 vs 请求**:没有 `id` 的是通知(如 `notifications/initialized`),不需要响应
- **stdout 是协议专线**:服务器的日志必须打到 stderr,混进 stdout 会污染协议流,客户端直接解析失败

### 4. MCP 服务器注册表

```python
MCP_SERVERS: dict[str, dict] = {
    "repo": {
        "description": "GitHub 风格仓库信息服务器(教学模拟)。提供 3 个工具。",
        "command": [sys.executable, "mcp_servers/repo_server.py"],  # 连接命令
        "conn": None,   # 连接对象,None = 未连接
        "tools": [],    # 握手后缓存的工具清单
    },
}
```

要接入一个新的 MCP 服务器,只需在 `MCP_SERVERS` 加一条——不用改任何其他代码。把 `command` 换成 GitHub 官方 MCP server 的启动命令(npx/docker),协议完全一样。

### 5. 教学模拟服务器:repo_server.py

本期新建运行时资源 `mcp_servers/repo_server.py`——一个独立的 MCP 服务器进程:

| 服务器工具 | 作用 | 参数 |
|---|---|---|
| `search_repositories` | 按关键词搜仓库(按 star 排序) | query |
| `get_repository_info` | 查单个仓库详情 | owner, repo |
| `list_issues` | 列出仓库的开放 issue | owner, repo |

它实现了 MCP 协议的服务器端(`initialize` / `tools/list` / `tools/call`),模拟数据覆盖 deepseek-ai 等仓库。生产中把它换成 GitHub 官方 MCP server,客户端一行代码不用改——**这正是协议的价值:换实现,不换接口。**

### 6. MCP 面板

启动和每轮对话后打印面板,直观看到服务器连接状态:

```
┌──────────────────────────────────────────────────────────┐
│  🔌 Alex MCP 面板(第 11 期新增)                          │
├──────────────────────────────────────────────────────────┤
│  MCP 服务器注册表: repo                                   │
│──────────────────────────────────────────────────────────│
│  repo  🟢 已连接  工具数=3
│    GitHub 风格仓库信息服务器(教学模拟)。...
│    工具: search_repositories, get_repository_info, list_issues
└──────────────────────────────────────────────────────────┘
```

### 7. 演示场景:盘点工具清单 + 查 deepseek-ai 的仓库情况

```
你(自动演示 1): 请帮我盘点可用的外部工具服务器:列出注册的 MCP 服务器、
                连接 repo、列出它提供的工具,汇报每个工具的用途
你(自动演示 2): 我想了解 deepseek-ai 组织在 GitHub 上的仓库情况,请通过 MCP 完成:
    1. 用 search_repositories 搜索 deepseek 相关仓库
    2. 挑最热门的一个仓库,用 get_repository_info 查详情
    3. 再用 list_issues 看看它有哪些开放 issue

[工具调用] list_mcp_servers()
[工具调用] mcp_connect({'server_id': 'repo'})
  → 启动子进程 + initialize 握手 + 拉到 3 个工具
[工具调用] mcp_list_tools({'server_id': 'repo'})
[工具调用] mcp_call_tool({'server_id': 'repo', 'tool_name': 'search_repositories',
                          'arguments': {'query': 'deepseek'}})
[工具调用] mcp_call_tool({'server_id': 'repo', 'tool_name': 'get_repository_info',
                          'arguments': {'owner': 'deepseek-ai', 'repo': 'DeepSeek-R1'}})
[工具调用] mcp_call_tool({'server_id': 'repo', 'tool_name': 'list_issues',
                          'arguments': {'owner': 'deepseek-ai', 'repo': 'DeepSeek-R1'}})

[Alex] 已通过 MCP 查到:deepseek-ai 最热门的是 DeepSeek-R1(约 92k star)...
```

注意:Alex 调这 3 个"GitHub 工具"时,**step11_mcp.py 里没有一行查仓库的代码**——全部逻辑在 repo_server.py 进程里,靠 stdio JSON-RPC 传过去。

## 工作原理

### MCP 的三层架构:连接层 + 协议层 + 注册表层

```
┌─────────────────────────────────────────────────────────┐
│  注册表层(MCP_SERVERS)                                 │
│  server_id → {description, command, conn, tools}        │
│  加服务器 = 加一条注册,Agent 代码零修改                  │
├─────────────────────────────────────────────────────────┤
│  连接层(_McpConnection)                                 │
│  subprocess.Popen(command) + stdin/stdout 管道           │
│  _send() 写一行 JSON / _recv() 读一行 JSON               │
│  stop() 关掉子进程,不留孤儿进程                          │
├─────────────────────────────────────────────────────────┤
│  协议层(JSON-RPC 2.0)                                  │
│  _request():带 id 发请求,只认配对的响应                  │
│  initialize 握手 → tools/list 拿清单 → tools/call 调工具 │
└─────────────────────────────────────────────────────────┘
```

三层各司其职:

| 层 | 职责 | 能做到 | 做不到 |
|---|---|---|---|
| 注册表 | 服务器发现 | 加/换服务器零代码 | 动态发现网络上的服务器(需其他 transport) |
| 连接 | 进程间通信 | 启动/收发/关闭子进程 | HTTP/WebSocket 传输(那是 MCP 的另外两种 transport) |
| 协议 | 标准消息格式 | 对话任何 MCP 服务器 | 理解工具语义(schema 只描述参数,不描述行为) |

**关键洞察**:第 05 期的 Function Call 解决了"LLM 怎么表达要调工具",本期 MCP 解决了"工具从哪来"。前者是 Agent 和 LLM 之间的约定,后者是 Agent 和外部世界的约定。

### 内置工具 vs MCP 工具:完整对比

| 维度 | 内置工具(第 05 期) | MCP 工具(本期) |
|---|---|---|
| 工具住哪 | Agent 进程里的函数 | 外部服务器进程里 |
| 加工具 | 改源码 + 注册 schema | 在注册表加一条连接命令 |
| 通信方式 | 进程内直接调用 | 跨进程 stdio JSON-RPC |
| 谁维护 | 自己写、自己维护 | 别人写、拿来就用 |
| 换供应商 | 重写工具函数 | 换 command 里的启动命令 |
| schema 格式 | OpenAI function schema | MCP inputSchema(两者同构) |
| 对 LLM 的暴露 | 直接进 TOOLS 列表 | 经由 mcp_call_tool 转一手 |
| 上下文占用 | 一个工具占一个 schema | 4 个 MCP 工具打天下 |
| 生命周期 | 常驻 | 连接/断开随需 |

有人会问:为什么不把 MCP 服务器的每个工具直接转成内置 schema 注册给 LLM?(其实很多框架就是这么做的)本期选择"统一入口 `mcp_call_tool`"是教学取舍:让"工具在外部"这个事实对 LLM 透明可见,连接、列工具、调工具三步分开演示,学员能看清协议的形状。

### 一轮 MCP 调用的完整流程

```
用户输入: "查一下 deepseek-ai 的仓库情况"
    │
    ▼
主 Agent LLM 决策: "这需要外部工具,先 list_mcp_servers 看看"
    │
    ▼  mcp_connect(server_id="repo")
    │  ① subprocess.Popen 启动 repo_server.py
    │  ② initialize 握手(交换协议版本、身份)
    │  ③ notifications/initialized 通知
    │  ④ tools/list 缓存 3 个工具的清单
    │
    ▼  mcp_call_tool("repo", "search_repositories", {"query": "deepseek"})
    │  ⑤ 组装 {"jsonrpc":"2.0","id":N,"method":"tools/call",...} 写入 stdin
    │  ⑥ 阻塞等 stdout,id 配对的那条响应
    │  ⑦ 取出 result.content[0].text → 塞回对话 messages
    │
    ▼  (LLM 继续决策:查详情、看 issue、汇总)
    │
    ▼
主 Agent 整理汇报:"DeepSeek-R1 最热门,92k star..."
    │
    ▼  程序退出 shutdown_mcp()
       ⑧ terminate 所有子进程,不留孤儿
```

### 为什么 Agent 需要 MCP?

| 没有 MCP(工具内置) | 有 MCP(本期) |
|---|---|
| 加能力 = 改代码 | 加能力 = 连一个服务器 |
| 工具越多 execute_tool 越臃肿 | Agent 常年 4 个 MCP 工具,后方无限扩展 |
| 别人的工具要抄进来 | 别人写好,接上就用 |
| 换供应商 = 重写 | 换启动命令 |
| 工具和 Agent 同生死 | 服务器独立升级,Agent 无感 |

本质上,MCP 是给 Agent 加了**标准插座**。第 05 期教会 Agent"用手",本期把手动成了"通用接口"——任何按 MCP 规范生产的工具服务器,都能即插即用。

## 变更内容

累积式:`step11 = step10 + MCP 协议`,记忆层/技能层/工具循环/TodoList/子代理/Agent Team 代码未改。

| 变更点 | 第 10 期 | 第 11 期 |
|---|---|---|
| 工具数量 | 16 | +4 → 20(list_mcp_servers / mcp_connect / mcp_list_tools / mcp_call_tool) |
| 新增数据结构 | — | `MCP_SERVERS`(服务器注册表)+ `_McpConnection`(stdio 连接类) |
| system prompt | + Team 指南 | + MCP 使用指南(内联注入) |
| 启动信息 | 团队面板 | + MCP 面板(服务器、连接状态、工具数) |
| 新增函数 | — | `_McpConnection` / `mcp_connect` / `mcp_list_tools` / `mcp_call_tool` / `list_mcp_servers` / `shutdown_mcp` / `print_mcp_panel` |
| 新增命令 | team / wake | + mcp(MCP 面板) |
| 演示场景 | 团队协作开发登录功能 | 换成本期专属:① 盘点 MCP 工具清单 ② 查 deepseek-ai 仓库情况 |
| 运行时资源 | — | + `mcp_servers/repo_server.py` + `skills/github/SKILL.md` |
| 退出行为 | 直接退出 | 先 shutdown_mcp() 关闭所有服务器子进程 |
| Team 代码 | _run_team_member / team_wake / ... | 保留不动,完全复用 |

文件:`code/step11_mcp.py`

## 试一试

1. **看 MCP 全流程**:启动程序,两个自动演示会先后跑——演示 1 连接 repo 服务器盘点工具清单,演示 2 用 `mcp_call_tool` 连调三次,最终 Alex 汇报仓库情况。
2. **手动连接**:输入 `mcp` 命令看 MCP 面板,再问 Alex"帮我通过 MCP 查一下 DeepSeek-Coder 的仓库详情",看它自己走 连接 → 调工具 的流程。
3. **看协议裸流**:单独运行 `python mcp_servers/repo_server.py`,手动逐行粘贴 JSON-RPC 消息(initialize → tools/list → tools/call),亲眼看协议一问一答。提示:日志在 stderr,协议在 stdout。
4. **验证"换服务器不改代码"**:把 `MCP_SERVERS["repo"]["description"]` 改成任意文字(假装换了个供应商),重启后 Agent 照样能连接调用——体会"注册表换条目,协议代码零修改"。
5. **对比内置工具**:问 Alex"现在几点"和"查 deepseek-ai 的 DeepSeek-V3 详情"——前者走进程内的 `get_current_time`,后者走跨进程的 `mcp_call_tool`,在 `history` 里看两者的 tool_calls 有何不同。
6. **给 repo 服务器加一个工具**:在 `repo_server.py` 的 `TOOLS` 和 `call_tool` 里加一个 `list_contributors`,重启 Agent——**Agent 一行代码没改,能力变多了**。这就是 MCP 最爽的地方。
7. **思考题**:如果服务器的日志误打到 stdout 会发生什么?为什么 MCP 结果要统一装进 `content` 数组而不是直接回文本?stdio transport 之外,MCP 还支持哪两种 transport(提示:streamable HTTP)?
