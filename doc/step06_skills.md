# 第 06 期:Skills 按需加载

## 问题

第 05 期 Alex 装上了"手",有了 2 个工具。但那期结尾埋了个伏笔:如果需要 20 个工具呢?

把 20 个工具的 JSON schema 全塞进 `TOOLS` 列表,每次 API 调用都要带着——哪怕这轮对话只是问"你好":

- **token 成本线性上涨**:每个工具 schema 约 100-200 token,20 个工具每次调用多付 2000-4000 prompt token,90% 的对话根本用不上它们
- **模型选择变慢变差**:选项越多,模型在"调哪个工具"上的判断越容易出错
- **知识也一样膨胀**:想让 Alex 精通"Git 排错""代码评审规范""部署流程",把文档全文塞进 system prompt?上下文窗口很快被吃光

本质问题:**能力是无限的,但上下文是有限的**。需要一个机制,让 Agent 像人一样——平时只记住"我会什么",用到时才翻开"操作手册"。

## 解决方案

### 1. 技能 = 目录 + SKILL.md

每个技能是一个目录,里面一份 SKILL.md。头部是 YAML frontmatter(只放两个必填字段),正文是给模型看的知识/流程:

```markdown
---
name: git-cheatsheet
description: Git 命令速查。当用户问"怎么撤销提交""怎么回退版本"等 Git 问题时使用。
---

# Git 速查

## 撤销与回退

| 场景 | 命令 |
|---|---|
| 撤销最后一次提交(保留修改) | `git reset --soft HEAD~1` |
| 回退到指定版本 | `git reset --hard <commit>` |
...
```

本期提供两个示例技能,代表两种典型形态:

| 技能 | 形态 | SKILL.md 里装什么 |
|---|---|---|
| `weather` | 流程型 | 使用流程 + 模拟规则 + 回复模板 + 边界 |
| `git-cheatsheet` | 知识型 | 命令速查表本身,加载后直接照着回答 |

### 2. 两级加载:索引常驻,全文按需

**第一级(启动时)**:扫描 `skills/*/SKILL.md`,只解析 frontmatter,把 `name + description` 列表追加到 system prompt 尾部:

```python
def load_skills_index() -> list[dict]:
    index = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        meta = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        if not meta.get("name"):
            continue  # 没有 frontmatter 的文件不算技能
        index.append({
            "name": meta["name"],
            "description": meta.get("description", ""),
            ...
        })
    return index
```

20 个技能的索引也就几百 token,常驻无压力。

**第二级(按需)**:新增 `load_skill` 工具。模型看到索引,判断当前任务与某个技能相关,自己决定加载:

```python
{
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "加载一个技能的完整内容。当当前任务与技能索引中某个技能相关时,先调用此工具拿到技能全文,再按技能内容回答。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名,如 weather、git-cheatsheet"}
            },
            "required": ["name"],
        },
    },
}
```

执行时读文件返回全文:

```python
if name == "load_skill":
    skill_md = SKILLS_DIR / arguments["name"] / "SKILL.md"
    return json.dumps({
        "skill": arguments["name"],
        "content": skill_md.read_text(encoding="utf-8"),
    }, ensure_ascii=False)
```

### 3. 复用第 05 期的工具循环

技能加载不需要新循环——`load_skill` 就是一个普通工具,天然走第 05 期的 `chat_with_tools()`:

```
用户: "Git 怎么撤销上次提交?"
  ↓ 模型看到 system prompt 尾部的技能索引
assistant: tool_calls=[load_skill(name="git-cheatsheet")]   ← 模型自己决定
  ↓ 执行: 读 skills/git-cheatsheet/SKILL.md 全文
tool: {"skill": "git-cheatsheet", "content": "# Git 速查..."}
  ↓ 第 2 次调用 API(带着技能全文)
assistant: "用 git reset --soft HEAD~1,撤销提交但保留修改……"
```

## 工作原理

### 为什么"索引 + 按需"是对的模式?

它对应人类的两种记忆:

- **索引 = 目录页**:Alex 知道"我会查天气、会 Git 速查"——这是几十字的常驻记忆
- **SKILL.md = 操作手册**:真要查天气了,才从书架抽出对应手册——这是几千字,用完即走(留在本轮 messages 里,随历史截断)

### 与"全塞进 system prompt"的对比

| 方案 | 2 个技能 | 20 个技能 | 200 个技能 |
|---|---|---|---|
| 全量塞 system prompt | ~1K token | ~10K token | 爆上下文 |
| Skills 两级加载 | 索引 ~100 token | 索引 ~500 token | 索引 ~4K token |

关键差异:**全量方案的成本随技能数线性增长且每轮都付;Skills 方案只付索引的钱,全文只在用到的轮次付一次**。

### frontmatter 的最小实现

SKILL.md 头部用 `---` 包围的 YAML,本期不引入 pyyaml,手工解析 `key: value` 行:

```python
def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if end is None:
        return {}
    meta = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta
```

约定:**目录名 = frontmatter 的 name**(`load_skill` 按目录名查找),避免再做一层名字映射。

### 技能加载后去哪了?

进 messages 的 `tool` 消息,和第 05 期的工具结果一样被纳入历史回灌。这意味着:

- 同一会话里连续问 3 个 Git 问题,技能全文只加载一次(模型看历史里已有)
- 超过 MAX_ROUNDS 轮后,技能结果随旧对话一起被截断——需要时模型会再调一次 `load_skill`,天然自愈

### SKILL.md 设计三原则

1. **description 写"什么时候用"而不是"这是什么"**——模型靠它做路由判断,"Git 命令速查"不如"当用户问怎么撤销提交、回退版本时使用"
2. **正文写给模型看,不是写给人看**——多用表格和步骤,少用散文;模型照着执行,冗余文字只会浪费 token
3. **写清边界**——"不编造精确到小时的预报"这类约束句,是防止技能被滥用的小保险

## 变更内容

累积式:`step06 = step05 + 技能索引 + load_skill 工具`,工具循环一行未改。

| 变更点 | 第 05 期 | 第 06 期 |
|---|---|---|
| 工具数量 | 2(get_current_time / calculate) | +1(load_skill) |
| system prompt | 人设 | 人设 + 技能索引(尾部追加) |
| 新增函数 | — | `parse_frontmatter` / `load_skills_index` / `build_skills_prompt` |
| 运行时资源 | templates/SOUL.md | + skills/weather/ + skills/git-cheatsheet/ |
| 新增命令 | tools | skills(查看技能索引) |
| 自动演示 | 问时间(工具循环) | 问 Git(技能加载全过程) |

文件:`code/step06_skills.py`

## 试一试

1. **看自动演示**:启动后问"Git 怎么撤销上次提交",观察 `load_skill(git-cheatsheet)` 被调用、全文注入、回答引用速查表里的 `git reset --soft HEAD~1`。
2. **问天气**:输入"北京今天天气怎么样?"——看模型路由到 weather 技能,并按模板回答(注意它应注明"模拟数据")。
3. **输入 skills**:打印技能索引,直观对比"索引几十字"和"全文几千字"的差距。
4. **连续追问**:再问"那已经 push 了怎么办?"——观察模型不再调 `load_skill`(历史里已有全文),省一次加载。
5. **自己写一个技能**:新建 `skills/python-review/SKILL.md`,frontmatter 写清 description(什么时候用),正文放你的代码评审 checklist。写完输入 `skills` 确认被识别,再问"帮我看看这段代码有什么问题"试试路由。
6. **思考题**:如果把 load_skill 的结果改写进 system prompt(而不是 tool 消息),会发生什么?(提示:trim_history 只保留 system,技能全文会永久占上下文——这正是两级加载要避免的)

## 下一期预告

第 07 期:记忆系统。Alex 现在有会话内的 history,但重启就忘——"上次说好用 Python 3.12"这种跨会话偏好需要长期记忆。下一期引入 `memory/` 目录 + 用户画像,做记忆可视化面板。
