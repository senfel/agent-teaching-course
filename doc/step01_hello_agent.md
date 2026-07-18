# 第 01 期:认识 Agent + 开工准备

## 问题

开始学 Agent 之前,有两道门槛拦住很多人:

1. **概念门槛**:LLM 和 Agent 到底什么区别?学 Agent 要先会什么?
2. **操作门槛**:API Key 怎么买?代码怎么跑起来?

这一期解决两件事:把概念讲清楚,把环境搭起来,跑通第一次模型调用。

## 解决方案

### 1. 概念对齐:LLM vs Agent

| | LLM | Agent |
|---|---|---|
| 本质 | 大语言模型(LLM),输入文本/图像/音频输出文本(现已多模态,如 GPT-4o 看图、Gemini 听声音) | 以 LLM 为大脑,能调用工具、保持记忆、自主循环 |
| 能力边界 | 只会"生成内容"(说、写、画),不能主动行动 | 能"说"也能"做"(调工具、读写文件、上网) |
| 类比 | 一个博学但无手脚的顾问 | 一个有手有脚的助理 |

后续 14 期,我们就是给 LLM 装上"手脚"和"脑子",让它变成 Agent。

### 2. 开工准备

#### 2.1 注册 DeepSeek 账号

- 访问 https://platform.deepseek.com/
- 用手机号注册,完成实名认证
- 进入控制台

#### 2.2 充值 API 额度

- DeepSeek 按 token 计费(1 元约可买 100 万 token)
- 建议先充 10 元,够学完整套课程

#### 2.3 创建 API Key

- 控制台 → API Keys → 创建新 Key
- 复制保存(只显示一次,丢了只能重建)
- 命名建议:`teaching-course`

#### 2.4 安装 Python

课程代码需要 Python 3.10 及以上。先确认是否已安装,在终端执行:

```bash
python3 --version      # macOS / Linux
python --version       # Windows
```

- 输出版本号 ≥ 3.10:跳过安装,直接到 2.5
- 提示 `command not found` 或版本过低:按下面平台安装

**macOS**(系统自带 Python 版本通常偏低,建议另装):

- 访问 https://www.python.org/downloads/ 下载最新 3.x 安装包,双击安装
- 或用 Homebrew:`brew install python@3.13`
- 安装后重开终端,再次执行 `python3 --version` 确认

**Windows**:

- 访问 https://www.python.org/downloads/ 下载安装包
- 安装时**务必勾选 "Add python.exe to PATH"**,否则命令行找不到 `python`
- 安装完成后重开 PowerShell / cmd,执行 `python --version` 确认

#### 2.5 项目环境

> **为什么要虚拟环境?** 虚拟环境(venv)给当前项目单独装一套依赖,和系统、其他项目互不污染。本课程从 01 期用到 15 期,依赖覆盖 openai / mcp / fastapi 等多个大库,不隔离的话版本会互相打架。删项目时只要删 `.venv` 目录即可干净卸载。

**macOS / Linux**:

```bash
# 1. 创建虚拟环境(只需一次)
python3 -m venv .venv

# 2. 激活(每次开新终端都要做一次)
source .venv/bin/activate

# 3. 安装依赖(激活后 pip 会装到 .venv 里,不污染全局)
pip install -r requirements.txt

# 4. 配置 API Key
cp .env.example .env
# 用文本编辑器打开 .env,把 DEEPSEEK_API_KEY 改成你刚才复制的真实 Key
```

**Windows**(PowerShell):

```powershell
# 1. 创建虚拟环境(只需一次)
python -m venv .venv

# 2. 激活(每次开新终端都要做一次)
.venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置 API Key
copy .env.example .env
# 用记事本打开 .env,把 DEEPSEEK_API_KEY 改成你刚才复制的真实 Key
```

> Windows 首次激活若报 `无法加载文件……因为在此系统上禁止运行脚本`,在 PowerShell 管理员模式执行一次:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
> 然后重试激活。

激活成功后,终端提示符前面会出现 `(.venv)`,说明你已在虚拟环境里。此时 `python` 和 `python3` 都指向虚拟环境里的 Python。

### 3. 第一次调用

```bash
python code/step01_hello_agent.py
```

> 没激活虚拟环境直接跑会报 `ModuleNotFoundError: No module named 'dotenv'`,因为依赖没装进来。看到这个报错就回到 2.5 激活环境。

程序会自动问三个问题,然后进入交互模式。

## 工作原理

DeepSeek 兼容 OpenAI 协议,所以我们用 `openai` SDK,只需改两个参数:

```python
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",   # 关键:指向 DeepSeek
)
```

调用流程:

```
你的代码 → openai SDK → HTTPS 请求 → DeepSeek 服务器 → 模型推理 → 返回 JSON → SDK 解析 → 你拿到文本
```

**为什么不用官方 `deepseek` SDK?**
- DeepSeek 没有独立 SDK,官方推荐用 `openai` SDK + 改 `base_url`
- 这样代码几乎零成本切换到 OpenAI、Moonshot、智谱等兼容厂商

## 变更内容

本期代码只做一件事:`step01_hello_agent.py`

- 初始化 `OpenAI` 客户端,指向 DeepSeek
- `chat()` 函数:封装单次对话
- `main()` 函数:自动问 3 个问题 + 进入交互模式
- 交互模式用 `prompt_toolkit` 替代内置 `input()`,支持完整退格(长文本和中文都能删到行首)、`←/→` 移动光标、`↑/↓` 翻历史、`Ctrl-C` 作废当前行而不退出程序
- 错误处理:Key 错误时给出明确提示

## 试一试

1. **改模型**:把 `.env` 里的 `DEEPSEEK_MODEL` 改成 `deepseek-reasoner`,对比回答差异
2. **看 token 消耗**:在 DeepSeek 控制台的"用量"页面,观察每次调用消耗多少 token
3. **问它时间**:问"现在几点",看它怎么回答(为第 05 期 Tool Use 埋伏笔)
4. **连续问三次同样问题**:看它回答是否一致(为第 03 期 history 埋伏笔)

## 下一期预告

第 02 期:单次调用 → 连续对话。你会发现这一期的程序"金鱼记忆"——问完就忘,敬请期待。
