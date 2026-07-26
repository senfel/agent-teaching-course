# 第 02 期:单次调用 → 连续对话

## 问题

第 01 期你已经能跑通第一次调用,但只要多问两句,就会露馅:

```
你: 我叫张三,是一名 Python 后端工程师
Alex: 你好,张三!
你: 我姓什么?我是做什么工作的?
Alex: 抱歉,我不知道你姓什么,也没法知道你的职业……
```

明明上一句刚说过,下一句就忘了——这就是 **"金鱼记忆"**。

根本原因:模型 API 是 **无状态的(stateless)**。你每次发请求,服务器都把它当成一次全新的对话,完全看不到你上一轮说了什么。这不是 DeepSeek 的毛病,OpenAI、Claude、Gemini 都一样。

这一期我们先把"连续对话"的形式搭起来(循环),再故意把这个痛点暴露出来,为下一期 history 做铺垫。

## 解决方案

### 1. 用 while 循环把"一问一答"串起来

第 01 期的交互模式已经是个 while 循环,但只是简单包了一层。这一期把它做规范:

```python
while True:
    user_input = input("你: ").strip()
    if user_input.lower() in ("quit", "exit", "q"):
        break
    answer, p, c = chat(user_input)
    print(f"Alex: {answer}")
```

形式上,已经是"连续对话"了。

### 2. 故意不传 history,暴露痛点

关键是 `chat()` 函数内部:

```python
def chat(user_input: str) -> tuple[str, int, int]:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": user_input}],  # ← 只传当前这一句
        max_tokens=1000,
    )
    ...
```

`messages` 里 **永远只有当前这一句**,模型看不到上一句。这就是"金鱼记忆"的代码层面的真相。

### 3. 加 token 计数,看到上下文是空的

DeepSeek 返回的 `response.usage` 里有 token 统计:

| 字段 | 含义 |
|---|---|
| `prompt_tokens` | 输入 token 数(你发给模型的) |
| `completion_tokens` | 输出 token 数(模型回的) |
| `total_tokens` | 两者之和 |

你会发现,本期里每次调用的 `prompt_tokens` 都很小(就一句话),因为我们没有传上下文。下一期加上 history 后,这个数字会越滚越大——那就是上下文长度问题的由来。

## 工作原理

### 为什么 API 是无状态的?

设计取舍。HTTP 协议本身是无状态的,模型服务为了能横向扩容(任意一台服务器都能处理任意一次请求),选择不在服务端保存对话。

**"连续对话"的幻觉,是客户端伪造出来的**:

- 客户端(你的代码)自己维护一份对话历史
- 每次请求,把整段历史一起发过去
- 模型读到完整上下文,看起来就像"记得之前说过什么"

所以"记忆"不是模型的功能,而是你代码里 `messages` 数组怎么组织的问题。下一期第 03 期,我们就动手把这份历史维护起来。

### 调用流程(与第 01 期一致)

```
你的代码 → openai SDK → HTTPS 请求(只带当前一句) → DeepSeek → 推理 → 返回 JSON + usage
```

相比第 01 期,唯一的差异是:我们在循环里反复调用,并读取 `response.usage`。

## 变更内容

累积式:`step02 = step01 + while 循环 + token 计数`,不重构前期代码。

| 变更点 | 第 01 期 | 第 02 期 |
|---|---|---|
| `chat()` 返回值 | 只返回回答字符串 | 返回 `(answer, prompt_tokens, completion_tokens)` |
| 自动演示 | 直接问 3 个独立问题 | 连续问两个有上下文依赖的问题,暴露"金鱼记忆" |
| 交互循环 | 简单包一层 | 规范化 + 支持 `quit` / `reset`(reset 故意不给功能,留作伏笔) |
| token 可视化 | 无 | 每次输出 + 会话汇总 |

文件:`code/step02_loop_no_memory.py`

## 试一试

1. **亲手复现金鱼记忆**:启动程序,先说"我叫张三,后端工程师",再问"我姓什么",看它能否答上。
2. **观察 prompt_tokens**:每次调用后打印的 `prompt=` 是多少?为什么一直这么小?下一期加上 history 后再回来对比。
3. **输入 reset**:程序会提示"本期尚未实现记忆"。这行代码是故意留的伏笔——第 03 期会真正实现它。
4. **思考题**:如果想让模型"记住"上一句,最朴素的办法是什么?(答案就是下一期:把上一轮的问答塞进 `messages` 里一起发过去)

## 下一期预告

第 03 期:短期记忆 history。我们维护一个 `messages` 列表,每次调用把完整对话历史回灌进去,让模型真正"记住"上一句。同时引入上下文长度限制——历史不能无限长,这是个新问题。
