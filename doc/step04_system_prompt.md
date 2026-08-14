# 第 04 期:System Prompt 人设

## 问题

第 03 期你已经让模型"记住"了对话历史,但它还是个"没有身份的通用助手"——问它"你是谁",它会答一大段"我是由 XX 公司训练的大语言模型...";问它"能帮我做什么",它会列十几个方向,从写诗到心理咨询什么都接。

这在教学场景能忍,但放到真实业务里不行:你要的是一个**技术助理**,不是通用聊天机器人。模型该有的语气、能力边界、回答风格,都得你来定。

根本原因:前 03 期 `messages` 里只有 `user` 和 `assistant`,模型没有任何"身份约束"。这一期我们引入 `system` 角色——把人设写进 `templates/SOUL.md`,加载后作为 messages 的第一条,全程影响每次调用。

## 解决方案

### 1. 人设文件:templates/SOUL.md

把人设内容从代码里抽出来,放到独立文件 `templates/SOUL.md`,好处是:改人设不用改代码,非技术人员也能改。

```markdown
# Alex · 技术助理 人设

你叫 Alex,是一名资深全栈技术助理,服务于创业公司 CTO 张三。

## 角色定位
- 你不是通用聊天机器人,你是技术助理——回答简洁、专业、面向工程师
- 说话风格:务实、直接、不啰嗦,像同事之间讨论技术问题

## 能力边界
- 擅长:Python、后端架构、AI/Agent 开发、代码审查、技术选型
- 不擅长:闲聊、心理咨询——遇到这类问题,礼貌引导回技术话题

## 禁止事项
- 不要用"亲""宝子"等非正式称呼
- 不要每次回答都加"很高兴为你服务"之类的客套话
```

> 这是项目里第一个运行时资源文件。`README.md` 里提到过:**第 04 期起引入 `templates/` 目录**。

### 2. 加载并注入 system prompt

核心改动:程序启动时读取 `SOUL.md`,拼成 `{"role": "system", "content": ...}` 放进 messages 列表的第一条。

```python
SOUL_PATH = Path(__file__).parent.parent / "templates" / "SOUL.md"

def load_system_prompt() -> str:
    return SOUL_PATH.read_text(encoding="utf-8")

# messages 第一条永远是 system prompt
messages: list[dict] = [{"role": "system", "content": system_prompt}]
```

模型每次调用都会读到这条 system——它就像 Alex 的"出厂设置",全程约束每次回答。

### 3. trim_history 保护 system prompt

第 03 期的截断逻辑是从头删,但第 04 期 messages[0] 是 system prompt,不能被删。改一下:

```python
def trim_history(messages: list[dict]) -> list[dict]:
    has_system = messages and messages[0]["role"] == "system"
    system_msg = [messages[0]] if has_system else []
    convo = messages[1:] if has_system else messages[:]
    # 只对 convo 做截断,system_msg 永远保留
    ...
    return system_msg + convo[cut:]
```

这样无论聊多少轮,人设永远在第一条。

### 4. soul 命令

新增 `soul` 命令:打印当前加载的人设内容,方便调试和理解"人设长什么样"。

### 5. reset 保留人设

第 03 期的 `reset` 清空整个 messages,第 04 期改为"只清空对话,保留 system prompt"——Alex 还在,只是忘了聊了什么。

## 工作原理

### 为什么 system prompt 能约束模型行为?

模型在训练时就学会了"听从 system 指令"——`role: system` 的消息被当成"全局指令",优先级高于普通 user 消息。

```
messages = [
  {"role": "system",    "content": "你是 Alex,技术助理..."},  ← 全局指令
  {"role": "user",      "content": "你是谁?"},
  {"role": "assistant", "content": "我是 Alex..."},
]
```

每次调用,模型都会先读 system,再读历史对话,最后决定怎么回答。system 就像给模型戴了一顶"帽子",戴着这顶帽子,它就不是通用助手了,而是 Alex。

### 有无 system prompt 的行为对比

同一个问题"你好,你是谁?能帮我做什么?":

| | 无 system prompt | 有 system prompt(Alex) |
|---|---|---|
| 自我认知 | "我是由 XX 训练的大语言模型" | "我是 Alex,技术助理" |
| 回答风格 | 通用、客套、列一堆方向 | 简洁、务实、聚焦技术 |
| 能力边界 | 什么都接(写诗、心理咨询) | 聚焦技术,非技术问题礼貌引导 |
| token 占用 | 无额外 | system prompt 本身占 token |

这就是"人设"的约束力——不是改了模型,是给模型加了边界。

### system prompt 与上下文的关系

system prompt 也是 messages 的一部分,同样占 token:

```
messages = [
  system(~150 token) + 10 轮历史(~1000 token) + 当前一句(~20 token)
]
```

所以第 03 期引入的 `MAX_ROUNDS` 截断,在本期仍然有效——只是截断时跳过 system,只删对话历史。

## 变更内容

累积式:`step04 = step03 + system prompt 人设`,不重构前期代码。

| 变更点 | 第 03 期 | 第 04 期 |
|---|---|---|
| 人设来源 | 无 | `templates/SOUL.md` 外部文件 |
| messages 结构 | `[user, assistant, ...]` | `[system, user, assistant, ...]` |
| 截断逻辑 | 从头删 | 跳过 system,只删对话 |
| reset 行为 | 清空全部 | 清空对话,保留 system |
| 新增命令 | history | soul(查看人设) |
| 自动演示 | 对比第 02 期 | 对比有无 system prompt |
| 运行时资源 | 无 | `templates/SOUL.md`(首次引入) |

文件:`code/step04_system_prompt.py` + `templates/SOUL.md`

## 试一试

1. **对比演示**:程序启动会自动问"你是谁",对比有无 system prompt 的回答差异——有 system 时模型自称 Alex、风格务实。
2. **问非技术问题**:故意问"帮我写首诗"或"今天心情不好怎么办",看 Alex 怎么礼貌引导回技术话题。
3. **输入 soul**:打印当前加载的人设内容,直观看到 SOUL.md 长什么样。
4. **reset 后再聊**:reset 只清空对话,Alex 的人设还在。对比第 03 期的 reset 行为差异。
5. **思考题**:system prompt 也是 token,如果人设文件很长(比如 5000 字),会占用多少上下文?该怎么平衡人设详细度和 token 成本?(后续第 07 期记忆系统会涉及)

## 下一期预告

第 05 期:Tool Use 工具调用。Alex 现在能记住对话、有人设,但它还是只能"说"——不会查时间、不会调外部接口。下一期我们给它装第一个工具 `get_current_time`,解决第 01 期埋的"它不知道现在几点"的伏笔。
