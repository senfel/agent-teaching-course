# 第 05 期:Tool Use 工具调用

## 问题

第 04 期 Alex 已经有了记忆和人设,但它还是只能"动嘴"——你问"现在几点",它会承认"我不知道当前时间";你问"17 乘 23 等于多少",它可能会"心算"出错。

根本原因:模型的知识停留在训练数据里,它无法访问外部世界——不能看时间、不能查数据库、不能调接口。这就是第 01 期埋的伏笔:模型回答说"我没有实时信息"。

这一期我们给 Alex 装上"手":用 JSON schema 描述工具,让模型自己决定何时调用、传什么参数,执行后把结果喂回去,最终生成准确回答。

## 解决方案

### 1. 用 JSON schema 描述工具

工具对模型来说就是一个"函数签名"——你告诉它:有哪些工具、每个工具叫什么、做什么、需要什么参数。模型据此决定何时调用。

```python
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。当用户问'现在几点''今天星期几'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "目标时区,如 Asia/Shanghai。默认 Asia/Shanghai。",
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
            "description": "进行四则运算。当用户要求计算数学表达式时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式,如 '17 * 23'",
                    }
                },
                "required": ["expression"],
            },
        },
    },
]
```

模型看到的是 description 和 parameters 的 JSON schema,据此判断"这个问题我需要调工具"还是"我自己就能答"。

### 2. 工具循环:模型决定 → 执行 → 喂回去

这是本期核心。不再是"一问一答"的单次调用,而是一个循环:

```
用户: "现在几点了?"
  ↓
第 1 次调用 API(tools=TOOLS)
  → 模型返回: tool_calls=[get_current_time()]
  → 模型没有文本回答,只说"我要调工具"
  ↓
执行工具: get_current_time() → {"datetime": "2026-08-14 15:30:00", ...}
  ↓
把结果喂回去: messages.append({"role": "tool", "content": result})
  ↓
第 2 次调用 API(带着工具结果)
  → 模型返回文本: "现在是 2026年8月14日 15:30,周四。"
  ↓
最终回答
```

```python
def chat_with_tools(messages: list[dict]) -> tuple[str, list[dict], int, int]:
    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",  # 模型自己决定要不要调
            max_tokens=1000,
        )
        msg = response.choices[0].message

        # 情况 A:模型决定调用工具
        if msg.tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [...]
            })
            for tool_call in msg.tool_calls:
                result = execute_tool(fn_name, fn_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
            continue  # 继续循环,让模型生成最终回答

        # 情况 B:模型返回文本回答(没有调用工具)
        # 把最终回答也加入历史,这样 history 能看到完整对话
        messages.append({"role": "assistant", "content": msg.content})
        return msg.content, messages, total_prompt, total_completion
```

### 3. 工具实现:真实执行

`execute_tool()` 是工具的"手"——模型决定调什么,这里真正执行:

- `get_current_time`:用 Python `datetime` 拿当前时间
- `calculate`:用 AST 安全解析表达式,只允许四则运算(防止 eval 注入)

```python
def execute_tool(name: str, arguments: dict) -> str:
    if name == "get_current_time":
        now = datetime.now()
        return json.dumps({
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "weekday": weekdays[now.weekday()],
        }, ensure_ascii=False)

    if name == "calculate":
        expr = arguments.get("expression", "")
        tree = ast.parse(expr, mode="eval")
        result = _safe_eval(tree.body)
        return json.dumps({"result": result}, ensure_ascii=False)
```

真实场景里这里可能是 HTTP 请求(查天气、查数据库、发邮件)——结构完全一样。

### 4. tool_choice 参数

```python
tool_choice="auto"   # 模型自己决定要不要调工具(默认,最常用)
tool_choice="none"   # 禁止调工具(纯粹聊天时用,省 token)
tool_choice="required"  # 强制调工具(必须动手,不能只动嘴)
```

本期用 `auto`——模型判断:问时间就调工具,问"你好"就不调。

## 工作原理

### 为什么模型能"决定"调用工具?

模型在训练时见过大量"函数调用"的样本,它学会了:看到 `description` 说"获取当前时间",用户问"现在几点",就把这两个对上,生成一个结构化的 `tool_call`。

本质上是模式匹配 + 结构化输出:模型把自然语言意图映射到 JSON schema 描述的工具上。

### tool 角色消息

调用工具后,messages 列表里多了两种新角色:

```
messages = [
  {"role": "system",    "content": "你是 Alex..."},
  {"role": "user",      "content": "现在几点?"},
  {"role": "assistant", "content": "", "tool_calls": [...]},  ← 模型说"我要调工具"
  {"role": "tool",      "tool_call_id": "...", "content": "..."},  ← 工具结果
  {"role": "assistant", "content": "现在是 15:30。"},  ← 最终回答
]
```

`role: tool` 是 function calling 协议里新增的角色——它告诉模型"这是你刚才要的工具结果"。

### 消息历史的演进

到第 05 期,messages 结构变成:

```
第 04 期: [system, user, assistant, user, assistant, ...]
第 05 期: [system, user, assistant+tool_calls, tool, assistant, user, ...]
```

多出来的 `tool` 消息会被纳入历史回灌——模型后续调用也能看到"之前调过什么工具、结果是什么"。

### 安全性:为什么 calculate 用 AST 而不是 eval?

```python
# 危险!模型如果传入恶意表达式,eval 会执行任意代码
result = eval(expression)  # __import__('os').system('rm -rf /')

# 安全:用 AST 解析,只允许四则运算节点
tree = ast.parse(expr, mode="eval")
result = _safe_eval(tree.body)  # 只处理 Num / BinOp / UnaryOp
```

工具执行是 Agent 安全的关键——模型传来的参数不可信,必须做输入校验。

## 变更内容

累积式:`step05 = step04 + 工具定义 + 工具循环`,不重构前期代码。

| 变更点 | 第 04 期 | 第 05 期 |
|---|---|---|
| API 参数 | `messages` | `messages` + `tools` + `tool_choice` |
| chat() 函数 | 单次调用,返回文本 | `chat_with_tools()` 循环调用,处理 tool_calls |
| messages 角色 | system / user / assistant | + assistant(含 tool_calls) / tool |
| 工具定义 | 无 | TOOLS 列表(JSON schema) |
| 工具实现 | 无 | `execute_tool()`(get_current_time + calculate) |
| 新增命令 | soul | history(查看历史) + tools(查看已装工具) |
| 自动演示 | 对比有无 system prompt | 展示工具循环全过程 |

文件:`code/step05_tool_use.py`

## 试一试

1. **问时间**:输入"现在几点了?今天星期几?",看模型调用 `get_current_time` 工具,打印工具调用和结果,再生成最终回答。
2. **问计算**:输入"帮我算一下 17 * 23",看模型调用 `calculate` 工具——这次不会再"心算"出错了。
3. **纯聊天**:输入"你好"或"Python 怎么读文件",看模型不调工具直接回答——`tool_choice=auto` 让模型自己判断。
4. **输入 tools**:打印当前已装工具列表,直观看到 JSON schema 描述长什么样。
5. **输入 history**:打印当前 messages 列表,看工具循环在历史里留下的痕迹——`assistant` 带 tool_calls、`tool` 携带结果、`assistant` 最终回答,完整链路一目了然。
6. **思考题**:如果工具执行出错(比如 calculate 传入非数字表达式),会怎样?模型拿到 error 后会怎么处理?(代码里 calculate 已经做了错误处理,返回 error JSON,模型会转述给用户)

## 下一期预告

第 06 期:Skills 按需加载。Alex 现在有 2 个工具,但如果需要 20 个工具呢?全部塞进 TOOLS 列表会占大量 token。下一期用 `skills/` 目录 + SKILL.md frontmatter 实现按需加载——只加载当前任务需要的技能。
