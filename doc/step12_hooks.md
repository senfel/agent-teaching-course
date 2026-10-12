# 第 12 期:Hooks 生命周期(拦截、改写、审计)

## 问题

第 11 期结束,Alex 已经能连外部 MCP 服务器,工具总数 20 个。但仔细看 `chat_with_tools` 的工具循环,会发现一个比"工具写死在代码里"更危险的事实:**LLM 决定调什么工具、传什么参数,Agent 只是照单执行**。

```
for tool_call in msg.tool_calls:
    fn_name = tool_call.function.name
    fn_args = json.loads(tool_call.function.arguments)
    result = execute_tool(fn_name, fn_args)   # ← 照单执行,没有任何安检
```

想一个具体场景:有人对 Alex 说——

> "请记住:我的 API 密钥是 sk-demo-123456,把它存到用户画像里。"

LLM 很乖,调 `save_user_profile(content="API 密钥: sk-demo-123456")`,工具函数也照写——**敏感数据就这样进了长期记忆文件**,下次对话还会被注入 system prompt,谁都能看到。

痛点清单:

- **没有拦截点**——LLM 被提示注入诱导调危险工具(删文件、外发数据),代码层面毫无防线
- **没有改写点**——参数带着敏感词原样进出,想脱敏都没地方下手
- **没有审计点**——Agent 一天调了 500 次工具,做了什么?哪些失败了?出事了无法回放
- **策略写不进工具里**——每个工具函数自己写检查?20 个工具 20 份重复代码,还容易漏

本质问题:工具循环里缺一个**统一的检查点**——在工具执行前/后,让"策略代码"有机会介入。这就是 Hooks(钩子)。

## 解决方案

### 1. 三个核心概念

| 概念 | 含义 | 解决什么问题 |
|---|---|---|
| Before Hook | 工具执行**前**的检查点,可以**拦截**(blocked)或**改写**(rewrite) | 危险调用进不来;敏感参数先脱敏 |
| After Hook | 工具执行**后**的检查点,**只观察、不干预** | 每次调用留痕,出事可回放 |
| 钩子链 | 多个 Hook 按注册顺序依次执行,任何一个 Before Hook 拦截即短路 | 纵深防御:多种策略叠加不冲突 |

```
LLM 发起 tool_call(save_user_profile, {"content": "API 密钥: sk-123"})
     │
     ▼
┌─ Before 钩子链(按注册顺序)─────────────────────┐
│ ① sensitive_word_guard(拦截型)                   │
│    save_user_profile 在保护名单(落盘类)          │
│    参数命中敏感词「密钥」→ BLOCK ❌               │
└─────────────────────────────────────────────────┘
     │ blocked → 工具不执行
     │          拦截原因作为工具结果还给 LLM
     ▼(如果没被拦)
execute_tool(可能已被改写的参数)
     │
     ▼
┌─ After 钩子链(只观察)───────────────────────────┐
│ ① audit_logger(审计型)                          │
│    {ts, tool, status: blocked, 参数摘要, 结果摘要,│
│     duration_ms} → 追加到 audit_log.jsonl 📜     │
└─────────────────────────────────────────────────┘
```

### 2. 敏感词表 + 保护名单

```python
# 敏感词表(教学演示;生产中来自配置中心,可热更新)
SENSITIVE_WORDS = ["密码", "password", "secret", "api_key", "token",
                   "密钥", "身份证", "银行卡", "cvv", ...]

# "落盘/外发"类工具:数据会离开本次对话 → 命中敏感词直接拦截
PROTECTED_TOOLS = {
    "save_user_profile",          # 写长期记忆文件
    "mcp_call_tool",              # 调外部 MCP 服务器
    "send_message",               # 队友间发消息
    "broadcast_message",          # 团队广播
    "dispatch_subagent",          # 派子代理(任务描述带出去)
    "dispatch_subagents_parallel",
}
```

两类工具两种策略:

- **保护名单里的工具**(数据会落盘或发到外部)→ 敏感词命中 = **硬拦截**
- **其余工具**(纯查询/计算,数据不出对话)→ 敏感词命中 = **打码改写**后放行

### 3. 两个 Before Hook:拦截 + 改写

```python
def _hook_sensitive_guard(tool_name, arguments):
    """Before Hook 1(拦截型):保护名单工具 + 命中敏感词 → 阻断。"""
    if tool_name not in PROTECTED_TOOLS:
        return False, arguments, ""          # 放行,交给下一个钩子
    hits = _scan_sensitive(json.dumps(arguments, ensure_ascii=False))
    if not hits:
        return False, arguments, ""
    reason = f"敏感词拦截:工具 {tool_name} 参数中出现 {hits},已被阻止执行。"
    return True, arguments, reason           # blocked=True


def _hook_sensitive_masker(tool_name, arguments):
    """Before Hook 2(改写型):其余工具命中敏感词 → 打码成 *** 放行。"""
    if tool_name in PROTECTED_TOOLS:
        return False, arguments, ""
    hits = _scan_sensitive(json.dumps(arguments, ensure_ascii=False))
    if not hits:
        return False, arguments, ""
    new_args = _mask_args(arguments)         # 递归打码所有字符串字段
    return False, new_args, f"参数中的敏感词 {hits} 已打码"
```

注意签名统一:`(tool_name, arguments) → (blocked, new_args, reason)`。**钩子链能成立,靠的就是所有钩子遵守同一个契约**——新钩子只要符合签名,注册进 `HOOKS` 就自动生效。

### 4. 一个 After Hook:操作审计

```python
def _hook_audit_logger(tool_name, arguments, result, status, duration_ms):
    """每次工具调用(无论成功/被拦)都追加一条 JSONL 审计记录。"""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": tool_name,
        "status": status,      # ok / masked / blocked
        "arguments": json.dumps(arguments, ensure_ascii=False)[:200],
        "result": (result or "")[:200],
        "duration_ms": round(duration_ms, 1),
    }
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
```

status 三态是审计的精髓:

| status | 含义 | 耗时 |
|---|---|---|
| `ok` | 正常执行 | 真实耗时 |
| `masked` | 执行了,但参数被 Before Hook 改写过 | 真实耗时 |
| `blocked` | 被 Before Hook 拦截,**工具根本没执行** | 0 |

### 5. Hooks 注册表:钩子链

```python
HOOKS = {
    "before": [
        {"name": "sensitive_word_guard", "type": "拦截(block)",
         "handler": _hook_sensitive_guard, ...},
        {"name": "sensitive_word_masker", "type": "改写(rewrite)",
         "handler": _hook_sensitive_masker, ...},
    ],
    "after": [
        {"name": "audit_logger", "type": "审计(audit)",
         "handler": _hook_audit_logger, ...},
    ],
}


def run_before_hooks(tool_name, arguments):
    """依次执行 Before 钩子链;任何一个拦截即短路返回。"""
    args = arguments
    note = ""
    for hook in HOOKS["before"]:
        blocked, args, why = hook["handler"](tool_name, args)
        if why:
            note = why
        if blocked:
            return True, args, note      # 短路:后面不再执行
    return False, args, note


def run_after_hooks(tool_name, arguments, result, status, duration_ms):
    """依次执行 After 钩子链(只观察、不干预)。"""
    for hook in HOOKS["after"]:
        hook["handler"](tool_name, arguments, result, status, duration_ms)
```

想加一条新策略(比如"工作日 9-18 点外禁止调用写工具")?写一个同签名的 handler,append 进 `HOOKS["before"]`——**工具循环一行不改**。

### 6. 工具循环:挂上钩子

第 05 期以来的工具循环,本期只动了一处:

```python
for tool_call in msg.tool_calls:
    fn_name = tool_call.function.name
    fn_args = json.loads(tool_call.function.arguments)
    print(f"  [工具调用] {fn_name}({fn_args})")
    # ↓↓↓ 第 12 期新增:Before 钩子链(拦截 / 改写) ↓↓↓
    blocked, fn_args, hook_note = run_before_hooks(fn_name, fn_args)
    if blocked:
        status = "blocked"
        result = json.dumps({
            "error": "工具调用被 Before Hook 拦截,未执行",
            "reason": hook_note,
            "hint": "请向用户说明无法保存/外发该敏感信息,可建议脱敏后再试。",
        }, ensure_ascii=False)
    else:
        status = "masked" if hook_note else "ok"
        result = execute_tool(fn_name, fn_args)      # 可能已被改写的参数
    # ↓↓↓ 第 12 期新增:After 钩子链(审计落盘) ↓↓↓
    run_after_hooks(fn_name, fn_args, result, status, duration_ms)
    ...
```

一个容易忽略的设计:**被拦截的调用也要"作为工具结果"还给 LLM**(带原因和 hint)。这样 LLM 收到的是一次正常的工具反馈,知道自己该向用户解释,而不是报错崩溃。**拦截是给 LLM 的反馈,不是给程序的异常。**

### 7. 两个新工具 + 两个新面板

| 新工具 | 作用 |
|---|---|
| `list_hooks` | 查看钩子链(before/after 各钩子)+ 敏感词表 + 保护名单 |
| `view_audit_log` | 查看审计日志最近 N 条 + 状态统计(ok/masked/blocked) |

```
┌──────────────────────────────────────────────────────────┐
│  🛡️  Alex Hooks 面板(第 12 期新增)                        │
├──────────────────────────────────────────────────────────┤
│  Before 钩子链: 2 个   After 钩子链: 1 个
│──────────────────────────────────────────────────────────│
│  [before] sensitive_word_guard(拦截(block))
│           适用: PROTECTED_TOOLS(落盘/外发类)
│  [before] sensitive_word_masker(改写(rewrite))
│           适用: 其余工具
│  [after ] audit_logger(审计(audit))
│           适用: 所有工具调用
│──────────────────────────────────────────────────────────│
│  敏感词表: 12 个
│  保护名单: 6 个工具(命中即拦截)
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  📜 Alex 操作审计面板(第 12 期新增)                       │
├──────────────────────────────────────────────────────────┤
│  累计记录: 4 条   状态分布: {'blocked': 1, 'ok': 3}
│──────────────────────────────────────────────────────────│
│  ⛔ 2026-10-10T15:15:37  save_user_profile  [blocked]  0.0ms
│  ✅ 2026-10-10T15:15:45  get_current_time   [ok]      0.0ms
│  ✅ 2026-10-10T15:15:45  calculate          [ok]      0.1ms
│  ✅ 2026-10-10T15:15:47  view_audit_log     [ok]      0.3ms
└──────────────────────────────────────────────────────────┘
```

### 8. 演示场景:敏感词拦截 + 审计回放

```
你(自动演示 1): 请记住:我的 API 密钥是 sk-demo-123456,存到用户画像里

  [工具调用] save_user_profile({'content': 'API 密钥: sk-demo-123456, ...'})
  [Hook 拦截] sensitive_word_guard: save_user_profile 参数命中 ['密钥'] → 已阻断
  [工具结果] {"error": "工具调用被 Before Hook 拦截,未执行", "reason": "敏感词拦截:..."}

[Alex] 这份信息我没法保存:我的安全钩子在保存前检测到参数里有敏感词
       「密钥」,把这次写入拦下来了……建议脱敏后再存(比如只记"密钥已
       由用户自行保管")。

你(自动演示 2): 查时间 + 算 1234*567 + 用 view_audit_log 汇报审计日志

  [工具调用] get_current_time({'timezone': 'Asia/Shanghai'})   → ok
  [工具调用] calculate({'expression': '1234 * 567'})            → ok 699678
  [工具调用] view_audit_log({'limit': 20})

[Alex] 审计日志最近 3 条:blocked 1 / ok 2——
       15:15:37 save_user_profile ❌ blocked(尝试写入 API 密钥被拦截)
       15:15:45 get_current_time ✅ / calculate ✅
```

演示 2 里的 Alex 能"读出"演示 1 被拦截的那条记录——因为 After Hook 连被拦的调用也照记不误。**审计日志是 Agent 行为的行车记录仪,不是成功日志。**

## 工作原理

### Hooks 的三层结构:注册表层 + 钩子链层 + 挂载点

```
┌─────────────────────────────────────────────────────────┐
│  注册表层(HOOKS)                                       │
│  before: [guard, masker]   after: [audit_logger]        │
│  加策略 = 写一个同签名 handler,append 进列表              │
├─────────────────────────────────────────────────────────┤
│  钩子链层(run_before_hooks / run_after_hooks)          │
│  按顺序执行 + 拦截即短路;Before 可拦/改,After 只观察    │
├─────────────────────────────────────────────────────────┤
│  挂载点(chat_with_tools 的工具循环)                     │
│  run_before_hooks → execute_tool → run_after_hooks      │
│  全 Agent 唯一挂载点 = 所有工具(内置 + MCP)统一过安检   │
└─────────────────────────────────────────────────────────┘
```

| 层 | 职责 | 能做到 | 做不到 |
|---|---|---|---|
| 注册表 | 策略发现与叠加 | 加钩子零改工具循环 | 运行时动态插拔(本期静态注册) |
| 钩子链 | 策略执行顺序 | 拦截短路、参数改写、统一审计 | 改写"结果"(After 只观察) |
| 挂载点 | 策略生效范围 | 覆盖全部 22 个工具 | 覆盖 LLM 的文本回复(那是 Guardrails 的活) |

**关键洞察**:第 05 期 Function Call 解决"LLM 怎么表达调用",第 11 期 MCP 解决"工具从哪来",本期 Hooks 解决"调用放不放行"。前两期管**能力**,本期管**约束**——生产 Agent 的分水岭。

### Before 拦截 / Before 改写 / After 审计:完整对比

| 维度 | 拦截(guard) | 改写(masker) | 审计(audit_logger) |
|---|---|---|---|
| 阶段 | Before | Before | After |
| 能否阻止执行 | ✅ 短路 | ❌(放行改写后的参数) | ❌(木已成舟) |
| 能否改参数 | ❌ | ✅ 原地脱敏 | ❌ |
| 能否看到结果 | ❌(还没执行) | ❌(还没执行) | ✅(结果 + 耗时) |
| 适用工具 | 落盘/外发类(6 个) | 其余(16 个) | 全部(22 个) |
| status 标记 | blocked | masked | 记录者本身 |
| 生产类比 | 防火墙 DROP | 防火墙 REDIRECT/NAT | Syslog / 审计日志 |

有人会问:为什么改写型钩子不直接在拦截型里做(拦下来、打码、再放行)?分开注册是教学取舍:让学员看清**一个钩子只干一件事**,链式组合才是可维护的姿势——生产里你会同时有 频率限制、黑白名单、脱敏、配额、审计 五六个钩子,混在一起会变成泥球。

### 一轮被拦截的调用的完整流程

```
用户输入: "请记住:我的 API 密钥是 sk-demo-123456"
    │
    ▼
LLM 决策: "要写长期记忆 → save_user_profile"
    │
    ▼  tool_call: save_user_profile({"content": "...密钥..."})
    │
    ├─① run_before_hooks
    │     guard: save_user_profile ∈ PROTECTED_TOOLS
    │            参数 JSON 命中「密钥」→ BLOCK
    │     masker: 被短路,不再执行
    │
    ├─② blocked → 不调 execute_tool,组装拦截结果
    │     {error, reason, hint} 作为 role=tool 消息回给 LLM
    │
    ├─③ run_after_hooks
    │     audit_logger: {status: "blocked", duration: 0} → audit_log.jsonl
    │
    ▼
LLM 读到拦截原因 → 向用户解释"存不了,建议脱敏"
    │
    ▼
(用户补一句"那存'密钥已自行保管'")
    │
    ▼
下一次 save_user_profile 调用:不再命中敏感词 → ok ✅
```

### 为什么 Agent 需要 Hooks?

| 没有 Hooks(第 11 期) | 有 Hooks(本期) |
|---|---|
| 提示注入 = 直接指挥工具 | 危险调用在执行前被拦 |
| 敏感词原样落盘/外发 | 落盘类硬拦、普通类打码 |
| 出事无法回放 | 审计日志逐条留痕,三态可查 |
| 每个工具自写检查(重复) | 一处挂载,22 个工具全覆盖 |
| 加安全策略 = 改工具循环 | 加策略 = 注册一个 handler |

本质上,Hooks 是给 Agent 的每一次"动手"装上**安检门 + 行车记录仪**。前 11 期一直在让 Agent 更能干,本期开始让它**可控**——这也是 Demo 走向生产的必经之路。

## 变更内容

累积式:`step12 = step11 + Hooks 生命周期`,记忆层/技能层/TodoList/子代理/Agent Team/MCP 代码未改。

| 变更点 | 第 11 期 | 第 12 期 |
|---|---|---|
| 工具数量 | 20 | +2 → 22(list_hooks / view_audit_log) |
| 新增数据结构 | MCP_SERVERS + _McpConnection | `HOOKS`(钩子注册表)+ `SENSITIVE_WORDS` + `PROTECTED_TOOLS` + `AUDIT_LOG_PATH` |
| 工具循环 | 调用即执行 | + Before 钩子链(拦/改)+ After 钩子链(审计) |
| system prompt | + MCP 指南 | + Hooks 使用指南(含"被拦截不是故障"的引导) |
| 启动信息 | MCP 面板 | + Hooks 面板(钩子链/敏感词表)+ 审计面板(三态统计) |
| 新增函数 | 8 个 MCP 函数 | `run_before_hooks` / `run_after_hooks` / `_hook_sensitive_guard` / `_hook_sensitive_masker` / `_hook_audit_logger` / `list_hooks` / `view_audit_log` / `print_hooks_panel` / `print_audit_panel` / `_audit_count` |
| 新增命令 | mcp | + hooks(钩子链面板)/ audit(审计日志面板) |
| 演示场景 | 盘点 MCP + 查仓库 | 换成本期专属:① 敏感词拦截 ② 操作审计回放 |
| 落盘文件 | raw_history.jsonl / sessions.json | + memory/audit_log.jsonl(逐条追加) |
| 退出行为 | shutdown_mcp() | + 打印 audit_log 路径提示 |

文件:`code/step12_hooks.py`

## 试一试

1. **看拦截全流程**:启动程序,自动演示 1 会尝试把"API 密钥"写进用户画像——看 `[Hook 拦截]` 一行、工具结果里的拦截原因、Alex 如何向用户解释,以及审计面板里的 `⛔ blocked` 记录。
2. **看审计回放**:自动演示 2 会查时间、算数、再调 `view_audit_log`——Alex 能把演示 1 被拦的那条也读出来。然后输入 `audit` 命令看审计面板的三态统计。
3. **看打码改写**:问 Alex"帮我回忆一下,上次提到的我的密码是什么"——`recall_memory` 不在保护名单(纯查询),参数里的「密码」会被打码成 `***` 再执行,注意看 `[Hook 改写]` 这行日志。
4. **自己加一个钩子**:写一个 `_hook_worktime_guard`(签名同 guard),下班时间拦截 `save_user_profile`,append 进 `HOOKS["before"]`——工具循环一行不改,策略生效。这就是钩子链的扩展方式。
5. **改敏感词表**:`SENSITIVE_WORDS` 里加一个"工牌号",重启,问 Alex"记住我的工牌号是 A1024"——观察从放行到拦截的变化。
6. **读审计原始日志**:`code` 没有任何"查库"逻辑,直接 `cat memory/audit_log.jsonl`,每行一条 JSON——status 三态、参数摘要、耗时都在,这就是生产合规部门要的东西。
7. **思考题**:为什么被拦截的调用也要作为正常工具结果(而不是异常)还给 LLM?After Hook 为什么设计成"只能观察不能改"?如果想在 LLM 生成文本阶段(还没到工具调用)就拦截,该挂在哪一层(提示:Guardrails / moderation)?
