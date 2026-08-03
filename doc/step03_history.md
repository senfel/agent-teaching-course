# 第 03 期:短期记忆 history

## 问题

第 02 期你已经能把一问一答串成循环对话了,但模型有"金鱼记忆"——你上一句说"我叫张三",下一句问"我姓什么",它答不上来。

根本原因:每次调用 `messages` 里只传当前这一句,模型看不到历史。这不是模型笨,是你没把历史喂给它。

这一期我们就动手维护一份 `messages` 列表,每次调用把完整对话历史回灌进去,让模型真正"记住"上一句。同时引入上下文长度限制——历史不能无限长,这是个新问题。

## 解决方案

### 1. 维护一个 messages 列表

核心改动:把 `chat()` 的入参从"一句话"变成"完整 messages 列表"。

```python
def chat(messages: list[dict]) -> tuple[str, int, int]:
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,  # ← 完整历史,不再只传当前一句
        max_tokens=1000,
    )
    ...
```

每次对话的流程:

1. 用户输入 → `{"role": "user", "content": 输入}` 加入 messages
2. 调用 API,把完整 messages 发过去
3. 模型回复 → `{"role": "assistant", "content": 回复}` 也加入 messages
4. 下一次调用,messages 里已经带着之前的全部问答

这样模型每次都能"看到"完整上下文,"金鱼记忆"问题解决。

### 2. 上下文长度限制:历史不能无限长

历史一直累积,`prompt_tokens` 会越滚越大,最终触发模型的上下文长度上限(DeepSeek 通常 64K token)。

本期用最简单的截断策略:

```python
MAX_ROUNDS = 10  # 1 轮 = 1 user + 1 assistant

def trim_history(messages: list[dict]) -> list[dict]:
    rounds = sum(1 for m in messages if m["role"] == "user")
    if rounds <= MAX_ROUNDS:
        return messages
    excess_rounds = rounds - MAX_ROUNDS
    cut = excess_rounds * 2  # 每轮 2 条
    return messages[cut:]
```

超过 10 轮时,删掉最早的一轮,保留最近 10 轮。简单粗暴,但有边界。

> 第 07 期会升级为 compact 压缩策略(把旧对话摘要成一段,而非直接删),本期先解决"有没有"的问题。

### 3. reset 和 history 命令

- `reset`:清空 messages 列表,重新开始
- `history`:打印当前 messages 列表,直观看到历史结构

## 工作原理

### 为什么回灌历史能让模型"记住"?

模型本身是无状态的,但每次调用都会读到你传入的完整 `messages`。只要历史在 messages 里,模型就能"看到"之前说过什么。

```
第 1 轮:
  messages = [user: "我叫张三"]
  → 模型回复 "你好,张三!"
  messages = [user: "我叫张三", assistant: "你好,张三!"]

第 2 轮:
  messages = [user: "我叫张三", assistant: "你好,张三!", user: "我姓什么?"]
  → 模型能看到前两条,答 "你姓张"
```

### token 增长可视化

对比第 02 期,本期的 `prompt_tokens` 会明显更大,因为历史一起发了过去:

| 轮次 | 第 02 期 prompt_tokens | 第 03 期 prompt_tokens |
|---|---|---|
| 第 1 轮 | ~10(一句话) | ~10(一句话) |
| 第 2 轮 | ~10(还是一句话) | ~30(含第 1 轮的问答) |
| 第 5 轮 | ~10 | ~150(含前 4 轮) |

这就是上下文长度问题的由来——历史越多,token 越贵,也越容易触顶。

## 变更内容

累积式:`step03 = step02 + messages[] 回灌 + 上下文长度限制`,不重构前期代码。

| 变更点 | 第 02 期 | 第 03 期 |
|---|---|---|
| `chat()` 入参 | `user_input: str`(一句话) | `messages: list[dict]`(完整历史) |
| 历史维护 | 无,每次独立调用 | 维护 messages 列表,每次回灌 |
| 上下文限制 | 无 | MAX_ROUNDS=10,超时截断最早轮 |
| reset 命令 | 提示"未实现" | 真正清空 messages |
| history 命令 | 无 | 打印当前 messages 列表 |
| token 观察 | 每次 prompt 都很小 | prompt 随历史增长而增大 |

文件:`code/step03_history.py`

## 试一试

1. **对比第 02 期**:先说"我叫张三,后端工程师",再问"我姓什么",这次模型应该能答上来。
2. **观察 prompt_tokens 增长**:多聊几轮,看 prompt_tokens 是不是越来越大?这就是上下文在增长。
3. **输入 history**:打印当前 messages 列表,直观看到 `[user, assistant, user, assistant, ...]` 的结构。
4. **超过 10 轮**:故意聊超过 10 轮,输入 history 看最早的轮是不是被截断了。
5. **思考题**:如果历史很长,直接删掉最早的一轮会丢失重要信息。有没有办法既保留要点又控制长度?(答案就是第 07 期的 compact 压缩)

## 下一期预告

第 04 期:System Prompt 人设。我们用 `templates/SOUL.md` 给 Agent 设定"技术助理 Alex"的角色,对比有无 system prompt 时的行为差异。同时 system prompt 会成为 messages 列表的第一条,进一步影响上下文。
