# 第 07 期:记忆系统

## 问题

第 03-06 期 Alex 已经有了会话内 history,但有个致命缺陷:**重启就忘**。

早上让 Alex 帮忙写代码,Alex 记住了你"用 Python 3.12""类型注解必须完整"——下午重启程序,这些全没了。下一轮对话,Alex 又问你"你用哪个 Python 版本?",像第一次见面一样。

更糟的是,如果同时在两个项目里跑同一个 Agent,它**不知道你是张三**还是**李四**——用户身份都漂着。本质问题:Alex 缺少**跨会话、跨项目的稳定记忆**。

短期记忆 history 解决的是"上一句说什么",但不能解决"我长期是谁、要什么、习惯什么"。

## 解决方案

### 1. 三层记忆架构

把 Agent 的记忆拆成三层,各司其职:

| 层级 | 介质 | 写入时机 | 生命周期 | 本期引入? |
|---|---|---|---|---|
| **短期** | messages[] history | 每轮对话实时 | 会话内(超轮截断) | 第 03 期已有 |
| **长期 / 用户画像** | `templates/USER.md` | 用户偏好变更时(Agent 调工具) | 永久,跨会话常驻 | **第 07 期新增** |
| **原始历史** | `memory/raw_history.jsonl` | 每轮对话落盘 | append-only,可审计回溯 | **第 07 期新增** |

再加一个**压缩机制**:会话内轮数过多时,把早期对话 LLM 摘要化,腾出上下文窗口(也是第 07 期新增)。

### 2. 用户画像 = 稳定偏好的 Markdown 文档

`templates/USER.md` 就是一份 Markdown,记录稳定偏好(角色、技术栈、回答风格、反偏好)。Agent 启动时把它追加到 system prompt,就能"记得"上次说过的偏好。

```markdown
# 用户画像(User Profile)

## 基础信息
- 角色:张三,创业公司 CTO
- 技术栈:Python(主力)、TypeScript、Go(学习中)

## 稳定偏好
- 代码风格:类型注解完整、函数不超过 50 行
- Python 版本:3.12(全项目统一)
- 包管理:pip + venv,不用 poetry / uv
- 回答风格:简洁、直接,代码 > 长段文字
```

关键:**这个文件 Agent 自己会改**。当你说"我开始用 uv 了",Agent 调用 `save_user_profile` 工具,自动更新对应章节。下次启动,新偏好就在系统提示里。

### 3. 原始历史 = append-only JSONL

每次会话结束,把 messages 全量追加到 `memory/raw_history.jsonl`:

```jsonl
{"ts": "2026-08-28T14:30:01", "session": "20260828-143001", "role": "user", "content": "我开始用 uv 了"}
{"ts": "2026-08-28T14:30:08", "session": "20260828-143001", "role": "assistant", "content": "好的,我记下了..."}
{"ts": "2026-08-28T14:30:12", "session": "20260828-143001", "role": "tool", "content": "{\"saved\": \"稳定偏好\", ...}"}
```

为什么 JSONL 而非 JSON 数组?
- append-only:每次只追加一行,不会因为单次写入失败搞坏整文件
- 流式友好:未来训练、检索、向量化都直接按行读
- 教学场景够用,生产环境通常换 SQLite / 向量数据库

**原始历史 ≠ 上下文**。文件里的全量是审计/回溯用的,真正进 LLM 上下文的还是 trim 后的 messages。

### 4. Compact = 自动摘要压缩

历史超过 `COMPACT_THRESHOLD_ROUNDS`(默认 8 轮)时,触发 compact:

```python
def compact_history(messages: list[dict]) -> list[dict]:
    """保留 system + 摘要 + 最近 MAX_ROUNDS(10)轮,中间的早期对话压成一段 assistant 摘要。"""
    # 1. 提取要压缩的早期 messages
    # 2. 调用 LLM 用 compact_prompt.md 的提示词生成摘要
    # 3. 替换为一条 [compact summary] 的 assistant 消息
    # 4. 保留最近 MAX_ROUNDS=10 轮对话原文
```

触发后生成的新结构:`[system, 摘要 assistant, ...最近 10 轮对话(含 tool/assistant)]`。

提示词模板独立放在 `templates/compact_prompt.md`,约束模型"只输出 500 字内的第三人称摘要,保留关键事实/偏好/未完成任务"。

### 5. 两个新工具

```python
# 写入用户画像(Agent 主动调用,用户偏好变化时)
save_user_profile(section="稳定偏好", new_content="包管理用 uv ...")

# 主动查询记忆(教学用,让 Agent 自查)
recall_memory(topic="我的 Python 版本")  # 返回 USER.md 中匹配的章节
```

这两个工具和第 05 期的 `get_current_time` / `calculate`、第 06 期的 `load_skill` **完全平等**——都走同一套工具循环。这就是累积式的好处:加能力 = 加工具,核心循环不变。

### 6. 记忆可视化面板

启动时打印一个面板,直观对比三种记忆的"状态":

```
┌──────────────────────────────────────────────────────────┐
│  🧠 Alex 记忆面板(第 07 期新增)                          │
├──────────────────────────────────────────────────────────┤
│  [长期记忆] USER.md 加载成功(823 字符)                   │
│    • 基础信息                                              │
│    • 稳定偏好                                              │
│    • 工作背景                                              │
│    • 已知反偏好                                            │
│──────────────────────────────────────────────────────────┤
│  [原始历史] raw_history.jsonl: 47 条记录(累计)            │
│  [上次会话] 2026-08-28 14:30  轮数=12  token=8432         │
└──────────────────────────────────────────────────────────┘
```

学员看一眼就懂:长期记忆常驻 / 原始历史累计 / 上次会话的元数据全在。

## 工作原理

### 为什么需要三层而不是一层?

| 方案 | 优点 | 缺点 |
|---|---|---|
| 只用 messages(短期) | 简单 | 重启即忘,无用户身份 |
| 只用 JSONL 文件(全量) | 永久 | 每轮全量回灌上下文,token 爆炸 |
| 只用 USER.md(长期) | 跨会话 | 丢失对话细节,无法审计 |
| **三层组合(本期)** | 各取所长 | 实现稍复杂 |

关键洞察:**短期是"工作记忆"(WM),长期是"陈述性记忆"(LTM),原始历史是"事件日志"(event log)**——和人脑三类记忆分工一致。

### Compact 的边界

什么时候 compact、什么时候 trim,要分清:

- **trim**(第 03 期已有):轮数超 MAX_ROUNDS(10)→ 直接**丢掉**早期消息。适合不需要的闲聊。
- **compact**(本期新增):轮数超 COMPACT_THRESHOLD_ROUNDS(8)→ 用 LLM **压缩**早期对话为摘要,再保留最近 MAX_ROUNDS(10)轮原文。适合有上下文的对话。

trim 是无损压缩?不,trim 是**有损丢弃**;compact 是**有损压缩**。两者权衡:trim 省时间但丢上下文,compact 慢一点但保留关键信息。

### save_user_profile 的写入策略

工具按"section"参数定位章节,**替换**整个章节内容而不是 append:

```python
# 输入:section="稳定偏好", new_content="..."
# 找到 ## 稳定偏好 开头,替换到下一个 ## 之前
# 章节不存在 → 追加到文件末尾
```

为什么是替换而非 append?因为 USER.md 是结构化文档,同主题只能有一个权威版本,append 会让"代码风格"出现两个互相矛盾的描述。

### recall_memory 的过滤逻辑

按 topic 简单匹配章节标题:

```python
topic = "Python 版本"
# 扫描 USER.md,匹配包含 "Python" 的章节
# 返回该章节内容
```

教学场景够用。生产环境通常用向量数据库(embedding + 语义检索)替代关键字匹配——这是第 09 期子代理/工具检索时会讲的进阶内容。

### 7. session_id 三种模式

启动时从环境变量读 session_id,本期有三种模式(注意:**session_id 只用作 raw_history.jsonl 里的审计标签,不实现真正的会话续接**——不隔离 raw_history、不影响 USER.md、不影响 system prompt、不影响 compact/trim):

| 模式 | 触发条件 | session_id 格式 | 用途 |
|---|---|---|---|
| **模式 1**(默认) | 不设 `AGENT_SESSION` | `auto-YYYYMMDD-HHMMSS` | 每次启动天然独立(单人随便聊) |
| **模式 2**(跨启动接续) | 设 `AGENT_SESSION=<name>` | `<name>` | 多次启动同 ID,仅审计层面同组 |
| **模式 3**(项目内开新轮) | `AGENT_SESSION=<name>` + `AGENT_SESSION_RESET=1` | `<name>-HHMMSS-<6位hex>` | 同名字但 UUID 后缀,标记"项目里开新会话" |

**重要的边界**:想做真正的会话续接(注入历史 messages),需要:
1. 按 session 隔离 raw_history(写到 `memory/sessions/<id>.jsonl`)
2. 启动时按 session 加载最近 N 条注入 messages
3. 按 session 维护独立 profile

本期只做"审计标签",不做"续接"——这是从"会记得"到"能续上"的下一阶段能力。

### 8. 事件溯源纪律

本期的核心实现纪律:**所有对话事件通过唯一的 `append_and_persist` 入口同步落盘**。

```python
# 唯一的对话事件入口(代码里只有这一处 messages.append + persist_one 绑定)
def append_and_persist(messages, msg, session_id, source="produced"):
    messages.append(msg)
    if session_id:
        persist_one(msg, session_id, source=source)
```

设计原则:
- **append 即写**:用户输入、assistant 回复、tool 结果进 messages 的同一刻,落盘 raw_history
- **不在退出/截断时补救**:trim/compact/quit 不再扫历史写盘——它们只重组 messages,不碰 IO
- **upsert sessions.json**:每轮对话后 log_session 覆盖更新同 session_id 的轮数/token,Ctrl-C 也有快照
- **失败不抛**:写盘异常被捕获,日志审计不能影响对话流程

事件溯源的好处:**任意时刻崩了,磁盘上的状态都是自洽的**,不用追"哪里没写"。

### 文件组织约定

```
agent-teaching-course/
├── templates/
│   ├── SOUL.md            ← Agent 人设(第 04 期)
│   ├── USER.md            ← 用户画像(第 07 期新增)
│   └── compact_prompt.md  ← 压缩提示词(第 07 期新增)
└── memory/
    ├── raw_history.jsonl  ← 原始历史(append-only)
    ├── sessions.json      ← 会话元数据
    └── user_profile.md    ← 画像副本(可选,镜像 templates/USER.md)
```

`templates/` 放"模板"(启动时加载),`memory/` 放"运行产物"(append-only)。分工清晰,易备份。

## 变更内容

累积式:`step07 = step06 + 三层记忆 + compact + 2 个工具`,工具循环代码未改。

| 变更点 | 第 06 期 | 第 07 期 |
|---|---|---|
| 工具数量 | 3 | +2 → 5(save_user_profile / recall_memory) |
| system prompt | 人设 + 技能索引 | + 用户画像(尾部追加) |
| 启动信息 | 仅打印技能索引 | + 记忆可视化面板 |
| 退出时 | 不落盘 | 不再需要落盘(每轮 append 已实时写入 `raw_history.jsonl`,`sessions.json` 每轮 upsert) |
| 新增函数 | — | `load_user_profile` / `append_raw_history` / `_extract_content` / `persist_one` / `append_and_persist` / `log_session` / `compact_history` / `trim_history` / `save_user_profile` / `recall_memory` / `print_memory_dashboard` / `read_recent_raw_history` |
| 新增配置 | — | `COMPACT_THRESHOLD_ROUNDS=8`(必须 < `MAX_ROUNDS=10`,启动期 assert) |
| 运行时资源 | templates/SOUL.md, skills/* | + templates/USER.md, templates/compact_prompt.md, memory/ |
| 新增命令 | quit/reset/history/soul/tools/skills | + memory, recall |
| 演示场景 | 问 Git(技能加载) | 查"我之前说过什么"(长期记忆)+ 更新偏好(记忆写入) |

文件:`code/step07_memory.py`

## 试一试

1. **看记忆面板**:启动程序,注意打印的 "🧠 Alex 记忆面板"——USER.md 章节、上次会话的元数据。
2. **第一次自动演示**:让 Alex 调 `recall_memory` 查"我之前说过什么",观察它读 USER.md 并引用"张三,CTO,Python 3.12"等偏好。
3. **第二次自动演示**:让 Alex 调 `save_user_profile` 记录新偏好"我开始用 uv 了",验证 USER.md 是否真的写入(可手动打开文件看)。
4. **跨会话验证**:**退出程序(quit)再重新启动**,问"我之前说过用什么包管理?"——Alex 会说"uv",这就是长期记忆生效。
5. **触发 compact**:连续问 9-10 个问题,观察 console 打印 `[compact] 触发自动压缩`,然后看 messages 里多了一条 `[compact summary]` 的 assistant 消息。
6. **查原始历史**:输入 `memory` 命令,看最近几条原始历史(JSONL 落盘内容)。
7. **思考题**:`save_user_profile` 如果改成 append 而非 replace,USER.md 会发生什么?(提示:用户偏好可能自相矛盾,模型下次启动时会困惑"我到底用 pip 还是 uv")

## 下一期预告

第 08 期:任务规划 TodoList。Alex 现在能记住过去,但面对复杂任务"帮我准备周会"还是会一口气写完——缺乏规划能力。下一期引入 todolist,让 Agent 能拆任务、标 in_progress、勾完成,真正做到"做事有条理"。
