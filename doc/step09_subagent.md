# 第 09 期:子代理 Subagent

## 问题

第 08 期 Alex 有了 TodoList——能拆任务、标进度、强制串行执行了。但遇到"需要两个专家同时看"的场景,Alex 还是无能为力。

你说"帮我审查这段代码,顺便整理成文档",Alex 会:先做代码审查,做完了再做文档整理。两个任务串行排队,总耗时 = 审查时间 + 整理时间。

更糟的是,两个任务挤在同一个上下文里:审查时看到的代码细节会污染文档整理的思路,文档整理时又要重新"回顾"一遍代码。上下文越堆越乱,token 越花越多。

本质问题:Alex 缺少**子代理能力**——不会派活给别人,不会给子代理设定权限边界,更不会让多个子代理同时工作。

## 解决方案

### 1. 三个核心概念

| 概念 | 含义 | 解决什么问题 |
|---|---|---|
| 独立上下文 | 子代理有自己的 messages,不复用主 Agent 的对话历史 | 避免"审查代码"的上下文污染"整理文档"的上下文 |
| 工具白名单 | 子代理只能用 allowed_tools 中的工具 | 子代理 LLM 看不到未授权工具 = 物理隔离,不是"提醒" |
| 并发派遣 | 多个子代理用 ThreadPoolExecutor 同时执行 | 审查 + 整理同时跑,总耗时 ≈ max(两者) |

```
主 Agent(Alex)
  │
  ├──dispatch_subagents_parallel──┐
  │                               │
  ▼                               ▼
子代理A: 代码审查员            子代理B: 文档整理员
  • 独立 messages               • 独立 messages
  • 工具白名单(3个)            • 工具白名单(3个)
  • 自己的工具循环              • 自己的工具循环
  │                               │
  └─────────结果回传──────────────┘
                │
                ▼
     主 Agent 汇总两个结果,回复用户
```

### 2. 三个新工具

```python
# 派遣单个子代理(同步,阻塞直到完成)
dispatch_subagent(agent_type="code_reviewer", task="审查这段代码:...")
# → 返回 {agent_type, result, prompt_tokens, completion_tokens, elapsed_seconds}

# 并发派遣多个子代理(真并发,总耗时 ≈ max)
dispatch_subagents_parallel(tasks=[
    {"agent_type": "code_reviewer", "task": "审查这段代码:..."},
    {"agent_type": "doc_writer", "task": "整理成技术文档:..."},
])
# → 返回 {dispatched: 2, elapsed_seconds: 8.5, results: [...]}

# 查看可用子代理类型及权限
list_subagents()
# → 返回 {available: [{type, description, allowed_tools}, ...]}
```

这三个工具和第 08 期的 `todo_create`、第 07 期的 `save_user_profile` **完全平等**——都走同一套工具循环。累积式的好处再次体现:加能力 = 加工具,核心循环不变。

### 3. 独立上下文:子代理有自己的 messages

主 Agent 派遣子代理时,子代理拿到的是**全新的 messages**,不复用主 Agent 的对话历史:

```python
def _run_subagent(agent_type: str, task: str) -> tuple[str, int, int]:
    config = SUBAGENT_TYPES[agent_type]

    # ① 独立上下文:全新 messages
    sub_system = load_system_prompt() + config["extra_prompt"]
    sub_messages = [
        {"role": "system", "content": sub_system},
        {"role": "user", "content": task},
    ]
    # sub_messages 里只有 system + task,没有主 Agent 的历史对话
    ...
```

为什么需要独立上下文?

| 共享上下文(错误做法) | 独立上下文(正确做法) |
|---|---|
| 审查代码的细节混进文档整理 | 各自干净,互不干扰 |
| 主 Agent 的历史越长,子代理 token 越多 | 子代理 token 固定,只含任务描述 |
| 两个子任务互相干扰 | 两个子任务完全隔离 |

关键设计:子代理的 system prompt = 主 Agent 人设(SOUL.md)+ 角色额外提示(extra_prompt)。子代理继承人设但叠加专家角色。

### 4. 工具白名单:物理隔离,不是提醒

工具白名单的核心设计:**不是在 prompt 里说"你不能用某某工具",而是直接从 TOOLS 列表里删掉未授权工具**。

```python
    # ② 工具白名单:只保留 allowed_tools 中的工具定义
    allowed = set(config["allowed_tools"])
    sub_tools = [t for t in TOOLS if t["function"]["name"] in allowed]
    # sub_tools 里只有 3 个工具,LMM 根本看不到其他 8 个
```

| 方式 | 实现 | LLM 能看到未授权工具吗 | 能调用吗 |
|---|---|---|---|
| 提醒式(错误) | prompt 里写"不要用 dispatch_subagent" | 能看到 | 能调用(只是可能不调用) |
| 白名单(正确) | 从 TOOLS 列表过滤掉 | 看不到 | 调不了(工具不存在) |

两个预定义子代理类型的权限设计:

| 子代理类型 | 允许工具(3个) | 禁止工具(8个) |
|---|---|---|
| `code_reviewer` | get_current_time, calculate, load_skill | todo_create, todo_update, todo_list, save_user_profile, recall_memory, dispatch_subagent, dispatch_subagents_parallel, list_subagents |
| `doc_writer` | get_current_time, calculate, load_skill | 同上 |

关键约束:
- 不能用 `todo_*`:只有主 Agent 管任务,子代理不管任务规划
- 不能用 `save_user_profile` / `recall_memory`:只有主 Agent 管记忆,子代理不改用户画像
- 不能用 `dispatch_subagent`:禁止递归派遣,避免子代理再派子代理导致无限嵌套

### 5. 并发派遣:ThreadPoolExecutor 真并发

```python
def dispatch_subagents_parallel(tasks: list[dict]) -> str:
    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = {}
        for i, t in enumerate(tasks):
            future = executor.submit(_run_subagent, t["agent_type"], t["task"])
            futures[future] = i

        for future in as_completed(futures):
            idx = futures[future]
            result, p, c = future.result()
            results[idx] = {...}
```

`ThreadPoolExecutor` 提供真正的线程级并发。每个子代理在自己的线程中运行,有独立的 `sub_messages` 和 `sub_tools`。

| 串行派遣 | 并发派遣 |
|---|---|
| 审查 10s + 整理 8s = 18s | max(10s, 8s) = 10s |
| 一个做完才做下一个 | 同时开始,同时(差不多)结束 |
| 简单,不需要线程池 | 需要 ThreadPoolExecutor |

**为什么有效?** DeepSeek API 调用是 I/O 密集型操作(发请求 → 等响应),Python 的 GIL 在 I/O 等待时会释放,所以线程级并发能真正加速。

### 6. 子代理面板

启动和每轮对话后打印面板,直观看到子代理的派遣记录:

```
┌──────────────────────────────────────────────────────────┐
│  🔀 Alex 子代理面板(第 09 期新增)                     │
├──────────────────────────────────────────────────────────┤
│  可用类型: code_reviewer, doc_writer                   │
│──────────────────────────────────────────────────────────│
│  code_reviewer  [并发] 审查这段代码:def fetch_user...   │
│    token=1253  耗时=8.5s  结果=420字符                   │
│  doc_writer      [并发] 把这段代码整理成一份技术文档...  │
│    token=980   耗时=8.5s  结果=380字符                   │
└──────────────────────────────────────────────────────────┘
```

### 7. 演示场景:并发派遣"代码审查员"+"文档整理员"

```
你: 请帮我做两件事:1. 审查这段代码,指出问题和改进建议
                   2. 把这段代码整理成一份技术文档

[工具调用] dispatch_subagents_parallel({'tasks': [
    {'agent_type': 'code_reviewer', 'task': '审查代码:...'},
    {'agent_type': 'doc_writer', 'task': '整理文档:...'}
]})

  [子代理] 并发派遣 2 个子代理: code_reviewer, doc_writer
    [子代理·code_reviewer] 工具调用: get_current_time({})
    [子代理·doc_writer] 工具调用: load_skill({'name': 'git-cheatsheet'})
    ...

┌──────────────────────────────────────────────────────────┐
│  🔀 Alex 子代理面板                                      │
├──────────────────────────────────────────────────────────┤
│  code_reviewer  [并发] 审查这段代码:...                 │
│    token=1253  耗时=8.5s  结果=420字符                   │
│  doc_writer      [并发] 把这段代码整理成...              │
│    token=980   耗时=8.5s  结果=380字符                   │
└──────────────────────────────────────────────────────────┘

[Alex] 代码审查结果:fetch_user 没有处理 uid 不存在的情况...
       文档整理结果:# 函数说明\n\n## fetch_user(uid)\n...
```

## 工作原理

### 子代理的三层架构:独立上下文 + 工具白名单 + 工具循环

子代理不是单个组件,而是三层协作:

```
┌─────────────────────────────────────────────────────────┐
│  独立上下文层(隔离)                                    │
│  sub_messages = [system + extra_prompt, user task]     │
│  不复用主 Agent 对话历史,子代理只看到自己的任务         │
│  特点:上下文隔离,审查和整理互不干扰                    │
├─────────────────────────────────────────────────────────┤
│  工具白名单层(权限)                                    │
│  sub_tools = [t for t in TOOLS if name in allowed]      │
│  从 11 个工具中过滤出 3 个,LMM 看不到其他 8 个         │
│  特点:物理隔离,不是"提醒",LLM 无法绕过                │
├─────────────────────────────────────────────────────────┤
│  子代理工具循环(复用第 05 期)                          │
│  while True:                                             │
│    LLM → 看 sub_messages + sub_tools → 决定调哪个工具    │
│    代码 → 执行工具、把结果塞回 sub_messages              │
│    LLM → 看到结果,决定下一步                            │
│  特点:和主循环完全一样的结构,只是工具集和消息不同      │
└─────────────────────────────────────────────────────────┘
```

三层各司其职:

| 层 | 职责 | 能做到 | 做不到 |
|---|---|---|---|
| 独立上下文 | 上下文隔离 | 防止子任务互相污染 | 子代理"忘记"主 Agent 的背景信息 |
| 工具白名单 | 权限控制 | 阻止子代理调用未授权工具 | 阻止子代理在 prompt 里"提到"未授权工具名 |
| 工具循环 | 驱动子代理 ↔ 工具交互 | 让子代理自主完成多步任务 | — |

**关键洞察**:工具白名单是"物理隔离"——子代理的 LLM 根本不知道 `dispatch_subagent` 这个工具的存在,不是"知道但被提醒不要用"。这比在 prompt 里写"你不能用某某工具"安全得多,因为 LLM 可能"忘记"提醒,但无法调用一个不存在的工具。

### 并发 vs 串行:时间对比

```
串行派遣:
  t=0s   ──────────────── 代码审查(10s) ────────────────┐
  t=10s                                                    ├── 文档整理(8s)
  t=18s                                                    │
  总耗时 = 10 + 8 = 18s                                    ┘

并发派遣:
  t=0s   ──────────────── 代码审查(10s) ────────────────┐
  t=0s   ──────────── 文档整理(8s) ────────────┐         │
  t=8s                                            │         │
  t=10s                                                     ┘
  总耗时 = max(10, 8) = 10s
```

### 为什么 Agent 需要子代理?

| 没有子代理 | 有子代理 |
|---|---|
| 多专家任务串行排队 | 并发执行,总耗时 ≈ max |
| 所有任务挤在同一上下文 | 上下文隔离,互不干扰 |
| Agent 自己做所有事 | Agent 派活给专家,自己汇总 |
| 无法给子任务设权限 | 工具白名单,物理隔离 |
| 单个 Agent 承担所有角色 | 按需派遣不同专家 |

本质上,子代理是给 Agent 加了**分身能力**。第 08 期的 TodoList 是"任务记忆"(把做什么从隐式变显式),第 09 期的子代理是"分身能力"(把"一个人干所有事"变成"派活给专家")。

### 一轮子代理派遣的完整流程

```
用户输入: "审查这段代码,顺便整理成文档"
    │
    ▼
主 Agent LLM 决策: "这需要两个专家,用 dispatch_subagents_parallel"
    │
    ▼  dispatch_subagents_parallel(tasks=[...])
ThreadPoolExecutor 启动 2 个线程
    │
    ├──→ 线程1: _run_subagent("code_reviewer", "审查代码:...")
    │      │  独立 messages = [system+审查角色, task]
    │      │  工具白名单 = [get_current_time, calculate, load_skill]
    │      │  while True: LLM → 工具调用 → 结果 → LLM → ...
    │      └→ 返回审查结果文本
    │
    ├──→ 线程2: _run_subagent("doc_writer", "整理文档:...")
    │      │  独立 messages = [system+文档角色, task]
    │      │  工具白名单 = [get_current_time, calculate, load_skill]
    │      │  while True: LLM → 工具调用 → 结果 → LLM → ...
    │      └→ 返回文档整理结果文本
    │
    ▼  as_completed 收集所有结果
主 Agent 收到两个子代理的结果
    │
    ▼
主 Agent LLM 汇总: "代码审查结果:... 文档整理结果:..."
    │
    ▼
回复用户
```

主 Agent 是"项目经理",子代理是"专家顾问"。项目经理派活、收结果、汇报;专家在权限范围内独立工作。

### 子代理注册表设计

```python
SUBAGENT_TYPES = {
    "code_reviewer": {
        "description": "代码审查员。审查代码质量...",
        "allowed_tools": ["get_current_time", "calculate", "load_skill"],
        "extra_prompt": "你是代码审查专家。你的职责:...",
    },
    "doc_writer": {
        "description": "文档整理员。整理文档结构...",
        "allowed_tools": ["get_current_time", "calculate", "load_skill"],
        "extra_prompt": "你是技术文档专家。你的职责:...",
    },
}
```

注册表是"角色定义"的集中管理:
- `description`:告诉主 Agent 这个子代理能做什么(用于 LLM 决策派谁)
- `allowed_tools`:工具白名单(硬约束,LLM 看不到未授权工具)
- `extra_prompt`:角色提示(叠加在 SOUL.md 之上,赋予专家身份)

要新增一个子代理类型,只需在注册表加一条,不用改任何其他代码。

### 子代理 vs 多角色 Prompt

| 维度 | 多角色 Prompt(一个 Agent 扮演多个角色) | 子代理(本期) |
|---|---|---|
| 上下文 | 共享,互相干扰 | 独立,互不干扰 |
| 工具权限 | 全部工具可用 | 工具白名单,物理隔离 |
| 执行方式 | 串行(一个角色做完换下一个) | 可并发(ThreadPoolExecutor) |
| token 消耗 | 上下文越来越长 | 每个子代理固定 token |
| 可扩展性 | 角色越多 prompt 越乱 | 注册表加一条即可 |

关键洞察:**子代理把"一个超级 Agent 扮演所有角色"拆成"多个专家 Agent 各司其职"**。这和第 07 期把"对话记忆"拆成"长期记忆 + 会话记忆"是同一个设计思路——分而治之。

## 变更内容

累积式:`step09 = step08 + Subagent + 3 个工具`,记忆层/技能层/工具循环/TodoList 代码未改。

| 变更点 | 第 08 期 | 第 09 期 |
|---|---|---|
| 工具数量 | 8 | +3 → 11(dispatch_subagent / dispatch_subagents_parallel / list_subagents) |
| 新增 import | — | `from concurrent.futures import ThreadPoolExecutor, as_completed` |
| system prompt | 人设 + 技能 + 用户画像 + TodoList 指南 | + 子代理使用指南(内联注入) |
| 启动信息 | 记忆面板 + TodoList 面板 | + 子代理面板(初始为空) |
| 新增函数 | — | `_run_subagent` / `dispatch_subagent` / `dispatch_subagents_parallel` / `list_subagents` / `print_subagent_panel` |
| 新增数据结构 | — | `SUBAGENT_TYPES`(注册表) / `_subagent_runs`(运行时记录) |
| 新增命令 | todo | + subagents / dispatch |
| 演示场景 | 规划"准备周会" + 推进第一个任务 | 并发派遣"代码审查员"+"文档整理员" + 单独派遣"文档整理员" |
| 运行时资源 | templates/todo_guide.md | 无新增(子代理指南内联,不单独建模板文件) |

文件:`code/step09_subagent.py`

## 试一试

1. **看并发派遣**:启动程序,自动演示并发派遣"代码审查员"+"文档整理员"——观察两个子代理同时运行,面板显示两个 [并发] 记录,总耗时 ≈ max(两者)。
2. **看单独派遣**:自动演示 2 让 Alex 单独派遣"文档整理员"——观察子代理面板新增一个 [单独] 记录。
3. **自由对话:让 Alex 并发派遣**:输入"帮我审查这段代码并整理成文档",看 Alex 是否自动调 `dispatch_subagents_parallel`。
4. **验证工具白名单**:问子代理"帮我创建一个任务列表",观察子代理无法调用 `todo_create`(因为它看不到这个工具)。
5. **查看子代理面板**:输入 `subagents` 命令,看可用类型和运行记录。输入 `dispatch` 查看手动派遣指南。
6. **思考题**:如果去掉工具白名单(子代理能用所有工具),会发生什么?如果允许子代理递归派遣(dispatch_subagent 在白名单里),会怎样?如果用多进程代替多线程,性能会更好吗?

## 下一期预告

第 10 期:Agent 工作流编排。Alex 现在能拆任务(第 08 期)、能派子代理(第 09 期),但"先做什么、后做什么、什么条件才做下一步"还靠 LLM 临场判断。下一期引入工作流编排:预定义步骤、条件分支、并行/串行编排——让 Agent 按流程干活,而不是"想到哪做到哪"。
