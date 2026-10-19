"""config.py — 初始化客户端、路径、常量

[第 01 期] OpenAI 客户端、项目根路径
[第 04 期] 人设/用户画像路径
[第 06 期] 技能目录路径
[第 07 期] 记忆目录路径
[第 08 期] TodoList 指南路径
[第 09-10 期] 本期无新增路径,复用全部已有配置
[第 11 期] MCP 服务器目录路径(repo_server.py)
[第 12 期] 操作审计日志路径(audit_log.jsonl,After Hook 落盘)
[第 13 期] 成本 telemetry 日志路径(usage_log.jsonl,每次 LLM 调用记账)

第 13 期说明:MODEL_REGISTRY / TIER_DEFAULT / BACKGROUND_MODEL / ROUTER_STATE
这些"多模型注册表 + 路由器状态"放到了 model_router.py(本期核心模块),
本文件只保留所有模块共用的基础设施(客户端 / 路径 / 阈值)。
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ============ 初始化客户端(与第 01-12 期一致)============
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)

# 兼容保留:旧代码展示用的默认模型名(现在真正的选择权在 model_router 的路由器手里)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

MAX_ROUNDS = 10  # 会话内短期记忆上限(轮数)

# ============ 路径(从 code/step13_model_router/ 往上三级到项目根)============
_PROJECT_ROOT = Path(__file__).parent.parent.parent
TEMPLATES_DIR = _PROJECT_ROOT / "templates"
MEMORY_DIR = _PROJECT_ROOT / "memory"
SKILLS_DIR = _PROJECT_ROOT / "skills"
MCP_SERVERS_DIR = _PROJECT_ROOT / "mcp_servers"
SOUL_PATH = TEMPLATES_DIR / "SOUL.md"
USER_PATH = TEMPLATES_DIR / "USER.md"
COMPACT_PROMPT_PATH = TEMPLATES_DIR / "compact_prompt.md"
TODO_GUIDE_PATH = TEMPLATES_DIR / "todo_guide.md"
RAW_HISTORY_PATH = MEMORY_DIR / "raw_history.jsonl"
SESSIONS_PATH = MEMORY_DIR / "sessions.json"
REPO_SERVER_PATH = MCP_SERVERS_DIR / "repo_server.py"
AUDIT_LOG_PATH = MEMORY_DIR / "audit_log.jsonl"  # 第 12 期:操作审计日志(After Hook 落盘)
USAGE_LOG_PATH = MEMORY_DIR / "usage_log.jsonl"  # 第 13 期:成本 telemetry(每次 LLM 调用记账)

# 压缩触发阈值(轮数);超过后自动调用 compact
COMPACT_THRESHOLD_ROUNDS = 8

# 配置约束:触发阈值必须 < 保留上限
assert COMPACT_THRESHOLD_ROUNDS < MAX_ROUNDS, (
    f"COMPACT_THRESHOLD_ROUNDS({COMPACT_THRESHOLD_ROUNDS}) 必须 < "
    f"MAX_ROUNDS({MAX_ROUNDS}),否则 compact 永远不切窗口"
)
