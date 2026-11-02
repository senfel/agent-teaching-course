# 第 15 期 · 部署上线 + 课程总结(最终期)

> 代码:`code/step15_deploy/`(累积式:step15 = step14 + 部署上线)
> 主题:Web UI · 流式响应(SSE) · Docker 部署

## 一、问题

前 14 期,Alex 一直活在**命令行里**:开一个终端,`python -m` 起进程,一句一句地和它对话。能力已经攒得很厚了 —— 记忆、工具、技能、子代理、团队、MCP、Hooks、多模型路由、目标驱动,一共 28 个工具躺在终端后面。

但到了实战这一步,有三个问题必须回答:

**痛点 1:命令行不是"产品"。**
没人会天天开终端和一个黑窗口聊天。真正能用起来,至少要能**在浏览器里**和一个舒服的界面对话 —— 这意味着要给前 14 期的纯逻辑套一层 Web 外壳。

**痛点 2:一次性吐出整段回答,体验像"打印报告"。**
第 14 期之前的所有调用都是 `stream=False`:等 LLM 把整段话算完,一次性 `print` 出来。用户盯着黑屏干等几秒十几秒,中间没有任何反馈。ChatGPT 那种"一个字一个字蹦出来"的打字机体验,才是现代对话产品的标配。

**痛点 3:光会跑不算上线,还得能交付、能复现。**
"在我机器上能跑"和"能部署"之间隔着一条河:依赖怎么装、路径怎么放、环境变量怎么进、记忆怎么持久化、换了环境会不会崩。把 Alex 交给别人用,不能指望别人照着 README 手搓一遍。

一句话:**前 14 期造的是"发动机",第 15 期要把它装进一辆"能上路、能交付的车"。**

## 二、解决方案

第 15 期做三件事,**一行不改前 14 期的 Agent 能力,只加"外壳 + 交付"**:

### 1. Web UI:FastAPI + 内嵌前端,双入口不改内核

把 14 期积累的能力包进一个 FastAPI 服务,六个端点:

| 端点 | 作用 |
|---|---|
| `GET /health` | 健康检查(返回存活状态 + 会话数) |
| `GET /goal` | 读当前目标契约 —— 第 14 期伏笔的 Web 版验证点 |
| `GET /sessions` | 会话列表(左侧栏):扫 `raw_history.jsonl` 按 `session` 聚合 |
| `GET /history/{session_id}` | 回放某会话的 user/assistant,并重建上下文用于续聊 |
| `GET /` | 内嵌前端(单文件 HTML,零外部依赖) |
| `POST /chat` | 对话核心,返回 SSE 流式响应 |

前端是一段**内嵌在 `web.py` 里的 HTML**(`INDEX_HTML`),深色聊天界面,`fetch('/chat')` 用 `res.body.getReader()` + `TextDecoder` 逐块读流。打开 `http://localhost:8000` 即用,不用装任何前端框架。

界面是**左右结构**:左侧「🗂 会话」栏列出历史会话(点 `＋` 开新会话),点任意一条从 `/history/{id}` 回放该会话记录;右侧是聊天区。**为什么不用单独建一张"会话表"?** 因为前 7 期开始攒的 `memory/raw_history.jsonl`(append-only 事件溯源)已经给每条记录打上了 `session` 字段,直接扫它按 `session` 聚合就是会话列表 —— 记忆层和 Web 层共用同一份真相,不另起炉灶。

**入口双轨:** `python -m step15_deploy` 默认起 Web,加 `--cli` 回命令行 —— 两个入口共用同一套 `build_system_prompt()`、同一套 `stream_chat_with_tools()`、同一套工具和记忆层。

### 2. 流式响应:SSE,边生成边到前端

LLM 调用升级为流式:路由层新增 `llm_call_stream()`(和 `llm_call` 唯一区别是 `stream=True` + `stream_options={"include_usage": True}`),`stream.py` 新增 `stream_chat_with_tools()` 把增量文本和工具状态作为事件逐个 `yield`,Web 层用 `StreamingResponse` 打包成 SSE 字节流推给浏览器。

```python
# stream.py 事件协议(每个 yield 是一个 dict,web.py 序列化成 SSE 的 data 行):
{"event": "delta",       "text": "...", "model": "deepseek-chat"}  # 文本增量
{"event": "tool_call",   "name": "...", "arguments": {...}}         # 准备调工具
{"event": "tool_result", "name": "...", "result": "..."}            # 工具结果
{"event": "done",        "text": "..."}                             # 本轮结束
```

**为什么选 SSE 而不是 WebSocket?** 聊天是"单向流 + 每轮结束"的场景:客户端发一条,服务端持续吐多个事件直到 `done`。SSE 只需要 HTTP,浏览器原生 `fetch` 流式读即可,不用维护 WebSocket 的双向长连接协议 —— 对"AI 打字机"这个需求,SSE 是最小够用的方案。

### 3. Docker 部署:一次构建,处处可跑

三件套交付:

```dockerfile
# Dockerfile 分层缓存:依赖最稳放最前,改代码只重建最后一层
FROM python:3.11-slim
COPY requirements.txt → pip install   # 依赖层(独立一层)
COPY code/step15_deploy/ templates/ skills/ mcp_servers/   # 运行时资源
CMD ["python", "-m", "step15_deploy"]  # 默认起 Web
```

```yaml
# docker-compose.yml:一键起服务,env 注入 API Key,memory 挂卷持久化
services:
  alex:
    build: .                # 用 Dockerfile 构建
    ports: ["8000:8000"]
    env_file: [".env"]      # API Key 走环境变量,不打进镜像
    volumes: ["./memory:/app/memory"]  # 记忆/审计/账单落宿主机,容器销毁不丢
```

`.dockerignore` 挡住 `.env`、`__pycache__`、`.venv`、`memory/` 等不该进镜像的东西。

## 三、工作原理

### 请求一图流

```
浏览器(http://localhost:8000)
   │  用户输入 → fetch POST /chat {message, session_id}
   ▼
FastAPI(app = FastAPI(...))
   │  chat() 读 message/session_id
   ▼
StreamingResponse(event_stream(), media_type="text/event-stream")
   │  ① _get_messages(session_id):按会话取(建)上下文,system 来自 build_system_prompt()
   │  ② append_and_persist:落盘 user 消息(与 CLI 同款事件溯源)
   │  ③ trim_history / compact_history:截断 + 压缩(复用记忆层)
   │  ④ stream_chat_with_tools(messages, session_id):流式工具循环
   │        └─ llm_call_stream(流式)→ yield delta/tool_call/tool_result/done
   │  ⑤ 每个事件 json.dumps → "data: {...}\n\n"
   │  ⑥ 最后 "data: [DONE]\n\n"
   ▼
浏览器 fetch + getReader() + TextDecoder 逐块读,handleEvent 渲染
```

### 流式工具循环(stream.py)与非流式(agent.py)是孪生

两套循环结构**完全一致**,唯一区别是"一次性返回" vs "边生成边吐":

| | 非流式 `chat_with_tools`(第 05 期) | 流式 `stream_chat_with_tools`(本期) |
|---|---|---|
| 循环结构 | LLM → 有工具?→ 执行 → 回灌 → 继续 | 相同 |
| Hook / 路由记账 | 过 Hooks + `llm_call` | 相同(Hooks + `llm_call_stream`) |
| 文本呈现 | 攒完整段,一次性返回 | 每个增量 `yield` 一个 `delta` 事件 |
| 工具状态 | `print` 到终端 | `yield` 一个 `tool_call`/`tool_result` 事件 |
| 结果 | 返回 answer | 生成器耗尽 = 本轮结束 |

流式里最麻烦的一个坑是**工具调用参数是分片到达的**:OpenAI 流式 `tool_calls` 的 `id`/`function.name`/`function.arguments` 会拆成多个 chunk,`stream.py` 用 `tool_calls.setdefault(tc.index, {...})` 按索引累积分片,攒齐后再执行 —— 这是"流式 + 工具调用"最容易翻车、也最能体现手感的地方。

### 事件溯源的延续

第 14 期伏笔回收的关键:整个课程 Alex 都是**事件溯源**式工作 —— 每轮产生 `user`/`assistant`/`tool` 消息就落盘到 `memory/raw_history.jsonl`。Web 版**没有破坏这个约定**:`stream_chat_with_tools` 内部的 `append_and_persist` 与 CLI 版完全相同,流式只是"边落盘边推给前端",不影响记忆的完整性。

### 会话列表与续聊:事件溯源的直接产品

Web 层没有单独建"会话表",而是直接把 `raw_history.jsonl` 扫一遍、按 `session` 字段聚合出列表 —— `_list_sessions()` 负责聚合,`_session_history()` 负责回放。真正关键的兜底是 `_ensure_session()`:

```python
def _ensure_session(session_id):
    if session_id in _SESSIONS:      # 进程内已建过,直接复用
        return _SESSIONS[session_id]
    messages = [{"role": "system", "content": build_system_prompt()}]
    for it in _session_history(session_id):   # 重启后从落盘历史回放
        messages.append({"role": it["role"], "content": it["content"]})
    _SESSIONS[session_id] = messages
    return messages
```

这一步解决了 Web 版最容易被忽略的坑:FastAPI 的进程内 `_SESSIONS` 在**服务重启后就没了**,但 Alex 是"有状态 Agent",不能每次重启都失忆。`_ensure_session` 从 `raw_history.jsonl` 把该会话的 user/assistant 回放进 messages,于是"刷新页面 / 重启服务后再点那条会话"依然能接着聊 —— 生产版把这份进程内状态换成 Redis/数据库,衔接方式完全不变。

### GOAL_STATE 跨请求:无状态 HTTP + 有状态 Agent

第 14 期埋的伏笔:"目标驱动的 Agent 挂进服务里,用户发一个模糊目标就下线,控制循环怎么跨 HTTP 请求继续跑?"

答案是:**目标契约 `GOAL_STATE` 是模块级全局变量,天然跨 HTTP 请求存活。** 用户在一个请求里让 Alex 订了目标,刷网页、再发一个请求,`GET /goal` 读到的还是同一个契约。`web.py` 用 `from . import goal` 直接读这个全局状态 —— 这就是"无状态 HTTP"承载"有状态 Agent"的衔接点(教学版用进程内内存,生产版把它换进 Redis/数据库即可,衔接方式不变)。

## 四、变更内容(相比第 14 期)

| 变更点 | 第 14 期 | 第 15 期 |
|---|---|---|
| 交互形态 | 只有 CLI 命令行 | + Web UI(FastAPI + 内嵌前端) |
| LLM 响应 | 一次性返回(非流式) | + 流式(`llm_call_stream` / SSE) |
| 工具循环 | `chat_with_tools`(非流式) | + `stream_chat_with_tools`(流式孪生) |
| system prompt | 内联在 main() 里拼装 | 抽出 `build_system_prompt()`,CLI/Web 共用唯一来源 |
| 文件结构 | 单文件脚本 | **模块化包** `step15_deploy/`(config/model_router/agent/stream/web/prompt/...) |
| 入口 | 直接跑脚本 | `python -m` 包入口(`__main__.py` 分发:默认 Web / `--cli`) |
| 部署 | 无 | + Dockerfile / docker-compose.yml / .dockerignore |
| 前端 | 无 | + 内嵌 HTML(SSE 流式渲染) |
| 会话列表/回放 | 无 | + /sessions 聚合 + /history/{id} 回放 + 侧栏续聊(扫 raw_history.jsonl) |
| 新端点 | 无 | + /health /goal /sessions /history/{id} / /chat(SSE) |
| 累积式 | step14 = step13 + 目标驱动 | step15 = step14 + 部署上线 |

前 14 期的全部 Agent 能力(三层记忆、技能、TodoList、子代理、Team、MCP、Hooks、多模型路由、目标驱动)**一行未动**,只是从"一个进程里的函数"变成"一个服务里的端点"。

## 五、试一试

先把服务跑起来:

```bash
cd code
python -m step15_deploy       # 默认:Web 服务
# 浏览器打开 http://localhost:8000
# 想回命令行: python -m step15_deploy --cli
```

1. **体验打字机流式**:在浏览器里问一个需要它写一段代码或长回答的问题,观察文字是**一个字一个字冒出来**的,而不是整块弹出;再点一下"🎯 目标"按钮,看看它读到的目标状态(空的或上一步订的契约)。
2. **让工具也实时播报**:问一个会触发工具的问题(比如"查一下现在几点"或让它调 `set_goal`),看界面里"🔧 调用工具… ✅ 完成"的气泡和文字流交织出现 —— 这就是 `stream_chat_with_tools` 把工具状态也当事件 yield 的效果。
3. **看会话列表并续聊**:左侧「🗂 会话」栏列出所有历史会话(读 `raw_history.jsonl` 聚合),点任意一条回放该会话的 user/assistant 记录;刷新页面或重启服务后再点它,上下文会从落盘历史重建、可以接着聊 —— `/sessions` + `/history/{session_id}` 把"事件溯源"变成了前端可见的会话列表。
4. **回命令行对比非流式**:`--cli` 起 CLI,发同一条消息,对比"一次性整段打印"和"浏览器逐字流"的体感差 —— 内核一样,外壳不同。
5. **验证 `goal` 跨请求存活**:在 Web 里用 `目标:` 前缀订一个目标,然后刷新页面或切到 `/goal` 端点 —— 契约还在,证明 `GOAL_STATE` 模块级全局变量跨 HTTP 请求幸存(第 14 期伏笔回收)。
6. **Docker 交付**:项目根目录 `cp .env.example .env`(填好 `DEEPSEEK_API_KEY`),然后 `docker compose up --build`,浏览器打开 `http://localhost:8000`;改几行 `web.py` 再 `docker compose up --build`,体会"依赖层缓存命中、只重建源码层"的分层缓存。

---

## 🏁 课程总结:15 期全栈串联

回头看这一路,Alex 从一个"只会调用 API 的婴儿"长成一个"能自主完成目标、还能对外服务的 Agent":

| 期 | 核心能力 | 一句话类比 |
|---|---|---|
| 01 | 认识 Agent + 开工 | 婴儿出生(能调用 API) |
| 02 | 连续对话(无记忆) | 金鱼记忆 |
| 03 | 短期记忆 history | 能记住上一句 |
| 04 | System Prompt 人设 | 有身份 |
| 05 | Tool Use 工具调用 | 有手 |
| 06 | Skills 按需加载 | 有技能库 |
| 07 | 记忆系统(三层) | 有脑子(跨会话记得) |
| 08 | 任务规划 TodoList | 有条理 |
| 09 | 子代理 Subagent | 有分身 |
| 10 | Agent Team 团队 | 有团队 |
| 11 | MCP 协议 | 有插座(外部工具即插即用) |
| 12 | Hooks 生命周期 | 有安检(拦截 + 改写 + 审计) |
| 13 | 多模型路由 | 有调度台(选模型 + 记成本) |
| 14 | 目标驱动 Agent | 有目标感(订契约 + 自主推进 + 验收) |
| **15** | **部署上线 + 课程总结** | **能上路(Web UI + 流式 + Docker)** |

**一条贯穿全课程的暗线**:每个阶段都在往"发动机"里加一个**正交的能力层**,而贯穿始终不变的是三条纪律 —— **事件溯源**(每轮消息产生即落盘,记忆完整可回放)、**统一入口**(工具走 execute_tool,LLM 走 llm_call/llm_call_stream,能力走 build_system_prompt)、**审计可观测**(审计日志管合规,usage_log 管成本,goal_log 管进度)。

第 15 期做的事,本质是**给这台装配了 14 期能力的机器,装上最后一块外壳**:Web UI 把它从黑窗口请进浏览器,SSE 流式让它有了"打字机"的产品体验,Docker 让它从"我机器上能跑"变成"一次构建、处处可跑"。至此,15 期完整闭环。

> 15 期到此全部结束。你可以把 `code/step15_deploy/` 当起点继续演进:把进程内 `_SESSIONS`/`GOAL_STATE` 换进 Redis,给 `/chat` 加鉴权,把内嵌 HTML 换成正式前端,接入真实的多供应商模型 —— 骨架已经搭好,往哪长取决于你。

---

*— END OF EPISODE 15 / END OF COURSE —*