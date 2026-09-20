# 第 10 期:Agent Team 团队协作

## 问题

第 09 期 Alex 有了子代理——能并发派遣"代码审查员"+"文档整理员"了。但子代理是**临时工**:派遣→运行→结果→销毁,没有持久状态。

你说"让前端和后端协作开发一个登录功能",Alex 会:唤醒前端→前端做完→唤醒后端→后端做完。但前端需要后端的接口定义才能继续,前端怎么告诉后端"我需要什么"?后端回复了,前端第二次被唤醒时还记得第一次做了什么吗?

子代理做不到:
- **无持久状态**——每次 dispatch 都是全新 messages,上次做了什么全忘了
- **单向通信**——子代理只能把结果返回给主 Agent,不能给其他子代理发消息
- **无消息队列**——没有 inbox 机制,队友之间无法异步传话

本质问题:Alex 缺少**持久团队能力**——不会组建有状态的团队,队友之间不能直接通信,不能多轮协作。

## 解决方案

### 1. 三个核心概念

| 概念 | 含义 | 解决什么问题 |
|---|---|---|
| 持久队友 | team member 的 messages 跨唤醒保持,不像 subagent 用完即弃 | 前端队友第二次被唤醒时,还记得第一次做了什么 |
| inbox 消息系统 | 每个队友有 inbox(消息队列),队友之间异步通信 | 前端给后端的 inbox 发消息"我需要登录接口格式",后端下次被唤醒时看到 |
| 消息广播 | broadcast_message 一次性给所有队友的 inbox 发消息 | "需求变更了"一条消息所有人收到 |

```
主 Agent(Alex)
  │
  ├── team_wake(frontend_dev, "设计登录页面") ──┐
  │                                              │
  │      ┌── send_message ────┐                 │
  │      │                     │                │
  │      ▼                     ▼                │
  │  frontend_dev            backend_dev         │
  │  • 持久 messages         • 持久 messages    │
  │  • inbox(消息队列)       • inbox            │
  │  • 通信工具(2个)         • 通信工具(2个)    │
  │  • wake_count=2          • wake_count=1     │
  │      │                     │                │
  │      └── inbox 异步消息 ───┘                │
  │                                              │
  ▼  主 Agent 汇总团队结果,回复用户
```

### 2. 五个新工具

```python
# 唤醒持久队友(上下文跨唤醒保持,inbox 消息自动注入)
team_wake(member_id="frontend_dev", task="设计登录页面的 HTML 结构")
# → 返回 {member_id, wake_count, result, prompt_tokens, completion_tokens, elapsed_seconds}

# 给指定队友的 inbox 发消息(主 Agent 或队友都能调)
send_message(to_member="backend_dev", content="我需要登录接口的请求和响应格式")
# → 返回 {sent, from, to, content, inbox_size}

# 向所有队友的 inbox 广播一条消息
broadcast_message(content="需求变更:登录功能需要增加验证码校验")
# → 返回 {broadcast, from, delivered_to, content}

# 读取某个队友的 inbox(不消费,仅查看)
read_inbox(member_id="frontend_dev")
# → 返回 {member_id, unread_count, messages}

# 查看团队成员列表及状态
list_team()
# → 返回 {members: [{member_id, description, inbox_unread, wake_count, has_context}, ...]}
```

这五个工具和第 09 期的 `dispatch_subagent` 等工具**完全平等**——都走同一套工具循环。累积式的好处再次体现:加能力 = 加工具,核心循环不变。

### 3. 持久队友:messages 跨唤醒保持

子代理和团队成员最核心的区别就是**持久上下文**:

```python
def _run_team_member(member_id: str, task: str) -> tuple[str, int, int]:
    config = TEAM_MEMBERS[member_id]

    # ① 初始化持久 messages(仅第一次唤醒时)
    if config["messages"] is None:
        sub_system = load_system_prompt() + config["extra_prompt"]
        config["messages"] = [{"role": "system", "content": sub_system}]
    # config["messages"] 跨唤醒保持!第二次唤醒时已经有了第一次的对话记录

    # ② inbox 注入:把未读消息拼到 task 前面,读后清空
    if config["inbox"]:
        inbox_text = "\n\n--- 收到的队友消息 ---\n" + ...
        config["inbox"].clear()  # 读后清空(已消费)

    # ③ 追加 task 到持久 messages(上下文累积)
    user_content = task + inbox_text
    config["messages"].append({"role": "user", "content": user_content})
    # 注意:append 到已有的 messages,不是新建 list
```

| 维度 | Subagent(第 09 期) | Team Member(本期) |
|---|---|---|
| 生命周期 | 临时:派遣→运行→销毁 | 持久:注册一次,可多次唤醒 |
| messages | 每次 dispatch 全新 `sub_messages = [...]` | 跨唤醒保持 `config["messages"]` 累积 |
| 第二次唤醒 | 全新上下文,不记得上次做了什么 | 看到第一次的对话记录,上下文累积 |
| 状态 | 无状态 | 有 inbox + wake_count + 持久 messages |
| 通信 | 单向(主→子→结果回传) | 双向(inbox 异步消息) |

关键设计:`TEAM_MEMBERS` 注册表是全局持久的。每个成员的 `messages`、`inbox`、`wake_count` 在整个程序运行期间保持。

### 4. inbox 消息系统:异步通信

每个队友有 inbox(消息队列),队友之间通过 `send_message` 异步通信:

```
时间线:
  t=0   前端被唤醒,需要后端接口
        → 前端调用 send_message(to_member="backend_dev", content="我需要登录接口格式")
        → backend_dev 的 inbox 增加一条消息

  t=5   后端被唤醒
        → inbox 消息注入到 task:"--- 收到的队友消息 --- [frontend_dev] 我需要登录接口格式"
        → inbox 清空(已消费)
        → 后端设计好接口,调用 send_message(to_member="frontend_dev", content="接口定义:...")
        → frontend_dev 的 inbox 增加一条消息

  t=10  前端再次被唤醒
        → inbox 消息注入:"--- 收到的队友消息 --- [backend_dev] 接口定义:..."
        → 前端看到后端的回复,继续对接 API
```

| 共享上下文(错误做法) | inbox 异步消息(正确做法) |
|---|---|
| 所有队友共享一个 messages | 各自有独立 messages + 共享 inbox |
| 前端的对话历史混进后端的上下文 | 上下文隔离,互不干扰 |
| 无法异步——必须同时在线 | 异步——发消息后对方下次唤醒时看到 |

inbox 的设计要点:
- **读后清空**:唤醒时注入 inbox 消息后立即 `clear()`,消息不会重复消费
- **不消费的查看**:主 Agent 可用 `read_inbox` 查看队友 inbox(不触发清空)
- **发送者标识**:主 Agent 调用时 sender = "team_lead",队友调用时 sender = 自己的 member_id

### 5. 消息广播:一条消息所有人收到

```python
def broadcast_message(content: str) -> str:
    sender = _current_member_id or "team_lead"
    delivered = []
    for member_id, config in TEAM_MEMBERS.items():
        if member_id == sender:
            continue  # 不给自己发
        config["inbox"].append({"from": sender, "content": content, ...})
        delivered.append(member_id)
    return json.dumps({"broadcast": True, "delivered_to": delivered, ...})
```

适用场景:需求变更通知、全局进度同步、紧急告警等。一条 `broadcast_message` 调用,所有队友的 inbox 同时增加一条消息。

### 6. 团队成员注册表

```python
TEAM_MEMBERS = {
    "frontend_dev": {
        "description": "前端开发工程师。负责页面结构、样式、交互逻辑...",
        "allowed_tools": ["get_current_time", "calculate", "load_skill",
                          "send_message", "broadcast_message"],  # 5个(含通信)
        "extra_prompt": "你是团队的前端开发成员...",
        "inbox": [],       # 持久 inbox
        "messages": None,  # 持久对话历史:None = 尚未被唤醒
        "wake_count": 0,
    },
    "backend_dev": {
        "description": "后端开发工程师。负责 API 接口、数据模型...",
        "allowed_tools": ["get_current_time", "calculate", "load_skill",
                          "send_message", "broadcast_message"],
        "extra_prompt": "你是团队的后端开发成员...",
        "inbox": [],
        "messages": None,
        "wake_count": 0,
    },
}
```

注册表字段对比:

| 字段 | Subagent(第 09 期) | Team Member(本期) |
|---|---|---|
| description | 有 | 有 |
| allowed_tools | 有(3个) | 有(5个,含通信工具) |
| extra_prompt | 有 | 有 |
| inbox | 无 | **有**(持久消息队列) |
| messages | 无(每次新建) | **有**(持久,跨唤醒) |
| wake_count | 无 | **有**(唤醒计数) |

要新增一个团队成员,只需在 `TEAM_MEMBERS` 加一条——不用改任何其他代码。

### 7. 工具白名单:队友也有权限边界

和第 09 期的子代理一样,团队成员也有工具白名单(物理隔离):

| 团队成员 | 允许工具(5个) | 禁止工具(11个) |
|---|---|---|
| `frontend_dev` | get_current_time, calculate, load_skill, send_message, broadcast_message | todo_create, todo_update, todo_list, save_user_profile, recall_memory, dispatch_subagent, dispatch_subagents_parallel, list_subagents, team_wake, read_inbox, list_team |
| `backend_dev` | 同上 | 同上 |

关键约束:
- 不能用 `todo_*`:只有主 Agent 管任务,队友不管任务规划
- 不能用 `save_user_profile` / `recall_memory`:只有主 Agent 管记忆
- 不能用 `dispatch_subagent` / `team_wake`:禁止队友再派队友,避免无限嵌套
- 不能用 `read_inbox` / `list_team`:这些是管理工具,只有主 Agent 查看
- **能用** `send_message` / `broadcast_message`:这是队友之间的通信能力

### 8. 团队面板

启动和每轮对话后打印面板,直观看到团队成员状态:

```
┌──────────────────────────────────────────────────────────┐
│  👥 Alex 团队面板(第 10 期新增)                      │
├──────────────────────────────────────────────────────────┤
│  团队成员: frontend_dev, backend_dev                   │
│──────────────────────────────────────────────────────────│
│  frontend_dev  inbox=0 唤醒=1 上下文=有                 │
│  backend_dev   inbox=0 唤醒=1 上下文=有                 │
│──────────────────────────────────────────────────────────│
│  未读消息(2 条):                                      │
│    frontend_dev→backend_dev: 我需要登录接口的格式       │
│    backend_dev→frontend_dev: 接口定义:POST /api/login   │
└──────────────────────────────────────────────────────────┘
```

### 9. 演示场景:前端组+后端组协作开发"用户登录功能"

```
你: 请帮我组建团队协作开发一个用户登录功能
    1. 先唤醒后端开发,让后端设计登录接口
    2. 再唤醒前端开发,让前端根据后端的接口对接页面
    3. 如果前端有问题,通过 send_message 给后端发消息,再唤醒后端回复

[工具调用] team_wake({'member_id': 'backend_dev', 'task': '设计登录接口...'})
  [team·backend_dev] 读取 0 条 inbox 消息
  [team·backend_dev] 工具调用: send_message({'to_member': 'frontend_dev', ...})

[工具调用] team_wake({'member_id': 'frontend_dev', 'task': '设计登录页面...'})
  [team·frontend_dev] 读取 1 条 inbox 消息  ← 看到后端的消息!
  [team·frontend_dev] 工具调用: send_message({'to_member': 'backend_dev', ...})

┌──────────────────────────────────────────────────────────┐
│  👥 Alex 团队面板                                      │
├──────────────────────────────────────────────────────────┤
│  frontend_dev  inbox=0 唤醒=1 上下文=有                 │
│  backend_dev   inbox=1 唤醒=1 上下文=有                 │
│  未读消息(1 条):                                      │
│    frontend_dev→backend_dev: 验证码字段怎么传?         │
└──────────────────────────────────────────────────────────┘

[Alex] 已组建团队开发登录功能:后端设计了 POST /api/login 接口...
```

## 工作原理

### Team Member 的三层架构:持久上下文 + inbox 注入 + 工具循环

团队成员不是单个组件,而是三层协作:

```
┌─────────────────────────────────────────────────────────┐
│  持久上下文层(累积)                                    │
│  config["messages"] 跨唤醒保持                          │
│  第一次唤醒: [system, user(task1)]                      │
│  第二次唤醒: [system, user(task1), assistant, user(task2)] │
│  特点:上下文累积,队友"记得"之前做了什么              │
├─────────────────────────────────────────────────────────┤
│  inbox 注入层(通信)                                    │
│  if config["inbox"]:                                   │
│    task = task + inbox 消息拼装                        │
│    config["inbox"].clear()                             │
│  特点:唤醒时自动读取未读消息,读后清空                 │
├─────────────────────────────────────────────────────────┤
│  队友工具循环(复用第 05 期,加通信工具)                │
│  while True:                                             │
│    LLM → 看 config["messages"] + sub_tools → 决策       │
│    队友 → 可调 send_message 给其他队友发消息           │
│    代码 → 执行工具、把结果塞回 config["messages"]       │
│  特点:和主循环结构一样,但用持久 messages + 受限工具    │
└─────────────────────────────────────────────────────────┘
```

三层各司其职:

| 层 | 职责 | 能做到 | 做不到 |
|---|---|---|---|
| 持久上下文 | 上下文累积 | 让队友"记得"之前做了什么 | 自动总结历史(需要 compact) |
| inbox 注入 | 异步通信 | 队友之间传话,唤醒时自动读取 | 消息持久化(程序退出后丢失) |
| 工具循环 | 驱动队友 ↔ 工具交互 | 让队友自主完成多步任务 | 调用未授权工具 |

**关键洞察**:Team Member 的 `messages` 是持久的(存在 `TEAM_MEMBERS` 注册表里),而 Subagent 的 `sub_messages` 是临时的(函数局部变量,函数结束即销毁)。这是持久队友和临时子代理的本质区别。

### Team Member vs Subagent:完整对比

| 维度 | Subagent(第 09 期) | Team Member(本期) |
|---|---|---|
| 生命周期 | 临时:dispatch→run→destroy | 持久:注册一次,可多次唤醒 |
| messages 位置 | 函数局部变量 | TEAM_MEMBERS 注册表(全局) |
| messages 持久性 | 每次全新 | 跨唤醒累积 |
| 通信方式 | 单向(结果回传) | 双向(inbox 异步消息) |
| 消息队列 | 无 | inbox(读后清空) |
| 并发支持 | ThreadPoolExecutor 真并发 | 串行唤醒(每次一个) |
| 唤醒计数 | 无 | wake_count |
| 上下文初始化 | 每次 dispatch 初始化 | 仅第一次唤醒初始化 |
| 工具白名单 | 3个(基础工具) | 5个(基础 + 通信) |
| 适用场景 | 独立任务(审查代码、整理文档) | 多轮协作(前端+后端开发) |

### 一轮团队协作的完整流程

```
用户输入: "让前端和后端协作开发登录功能"
    │
    ▼
主 Agent LLM 决策: "这需要团队协作,用 team_wake"
    │
    ▼  team_wake(member_id="backend_dev", task="设计登录接口")
_run_team_member("backend_dev", task)
    │  ① messages 为 None → 初始化持久 messages
    │  ② inbox 为空 → 不注入
    │  ③ 追加 task 到 messages
    │  ④ 工具白名单:5个(含 send_message, broadcast_message)
    │  ⑤ 工具循环:LLM → 设计接口 → send_message 给前端发接口定义
    │  ⑥ 返回结果
    │
    ▼  team_wake(member_id="frontend_dev", task="设计登录页面")
_run_team_member("frontend_dev", task)
    │  ① messages 为 None → 初始化持久 messages
    │  ② inbox 有 1 条消息 → 注入到 task → 清空 inbox
    │     task 变成: "设计登录页面\n--- 收到的队友消息 ---\n[backend_dev] 接口定义:..."
    │  ③ 追加 task 到 messages
    │  ④ 工具循环:LLM → 看到后端接口 → 设计页面 → send_message 给后端问问题
    │  ⑤ 返回结果
    │
    ▼  (如果前端有问题,主 Agent 再次唤醒后端)
team_wake(member_id="backend_dev", task="前端问:验证码字段怎么传?")
_run_team_member("backend_dev", task)
    │  ① messages 已有内容(第二次唤醒)→ 不初始化,直接追加
    │  ② inbox 有 1 条消息 → 注入 → 清空
    │  ③ 持久 messages 现在: [system, user(task1), assistant, user(task2+inbox)]
    │     ↑ 后端记得第一次设计了什么!
    │  ④ 工具循环:LLM → 回复前端的问题 → send_message 回复
    │  ⑤ 返回结果
    │
    ▼
主 Agent 汇总团队结果,回复用户
```

主 Agent 是"项目经理",团队成员是"持久队友"。项目经理唤醒队友、收结果、汇报;队友在权限范围内独立工作,通过 inbox 互相传话。

### 为什么 Agent 需要持久团队?

| 没有团队(只有子代理) | 有团队(本期) |
|---|---|
| 多轮协作做不到(子代理无状态) | 队友记得之前做了什么 |
| 队友之间不能直接通信 | inbox 异步消息,双向通信 |
| 每次都要从零开始 | 上下文累积,协作效率高 |
| 无法广播通知 | broadcast 一条消息所有人收到 |

本质上,Agent Team 是给 Agent 加了**组建团队的能力**。第 09 期的子代理是"临时分身"(用完即弃),第 10 期的团队是"持久队友"(长期协作)。

## 变更内容

累积式:`step10 = step09 + Agent Team + 5 个工具`,记忆层/技能层/工具循环/TodoList/子代理代码未改。

| 变更点 | 第 09 期 | 第 10 期 |
|---|---|---|
| 工具数量 | 11 | +5 → 16(team_wake / send_message / broadcast_message / read_inbox / list_team) |
| 新增数据结构 | — | `TEAM_MEMBERS`(注册表) / `_current_member_id`(发送者标识) |
| system prompt | + 子代理指南 | + Agent Team 使用指南(内联注入) |
| 启动信息 | 子代理面板 | + 团队面板(显示成员、inbox、唤醒次数) |
| 新增函数 | — | `_run_team_member` / `team_wake` / `send_message` / `broadcast_message` / `read_inbox` / `list_team` / `print_team_panel` |
| 新增命令 | subagents / dispatch | + team / wake |
| 演示场景 | 并发派遣子代理 | 前端+后端协作开发"用户登录功能" + 广播需求变更 |
| 子代理代码 | _run_subagent / dispatch_subagent / ... | 保留不动,完全复用 |

文件:`code/step10_team.py`

## 试一试

1. **看团队协作**:启动程序,自动演示前端+后端协作开发"用户登录功能"——观察 `team_wake` 逐个唤醒队友,队友之间通过 `send_message` 通信,inbox 消息在下次唤醒时自动注入。
2. **看消息广播**:自动演示 2 用 `broadcast_message` 通知需求变更——观察一条消息同时进入所有队友的 inbox,然后唤醒后端确认收到。
3. **验证持久上下文**:连续唤醒同一个队友两次,问它"你刚才做了什么"——观察队友能回答(因为 messages 跨唤醒保持)。
4. **验证 inbox 异步通信**:让前端给后端发消息,不立即唤醒后端,用 `read_inbox` 查看后端 inbox——观察消息在 inbox 里等着。
5. **查看团队面板**:输入 `team` 命令,看团队成员的 inbox 消息数、唤醒次数、上下文状态。输入 `wake` 查看手动唤醒指南。
6. **对比子代理和团队**:先 `dispatch_subagent` 派一个子代理,再 `team_wake` 唤醒一个队友——观察子代理是"临时"的(用完即弃),队友是"持久"的(上下文累积)。
7. **思考题**:如果去掉 inbox(队友不能互发消息),团队协作会退化成什么?如果允许队友调用 `team_wake`(嵌套唤醒),会怎样?如果团队有 10 个成员,怎样编排唤醒顺序最高效?
