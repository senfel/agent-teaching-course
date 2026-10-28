"""prompt.py — System Prompt 人设层 + Skills 技能层

[第 04 期] 人设:从 templates/SOUL.md 加载 system prompt
[第 06 期] 技能:扫描 skills/ 目录,构建技能索引提示词(按需 load_skill)
"""
from .config import SOUL_PATH, USER_PATH, SKILLS_DIR

# ---- Alex 回答高亮(第 14 期):浅蓝底黑字,控制台一眼认出"这是它说的话" ----
_ANSI_ALEX_BG = "\033[30;48;5;153m"   # 256 色 153 号浅蓝底 + 黑字
_ANSI_RESET = "\033[0m"


def print_alex(text: str) -> None:
    """Alex 的回答统一浅底高亮打印 —— 宿主日志无底色,Alex 话有底色。"""
    print(f"{_ANSI_ALEX_BG}{text}{_ANSI_RESET}")


def load_system_prompt() -> str:
    """读取 SOUL.md(第 04 期)作为人设核心。"""
    if not SOUL_PATH.exists():
        raise FileNotFoundError(f"找不到人设文件: {SOUL_PATH}")
    return SOUL_PATH.read_text(encoding="utf-8")


def load_user_profile() -> str:
    """读取 templates/USER.md 作为长期记忆 / 用户画像(第 07 期)。"""
    if not USER_PATH.exists():
        return ""
    return USER_PATH.read_text(encoding="utf-8")


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}
    meta = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def load_skills_index() -> list[dict]:
    if not SKILLS_DIR.exists():
        return []
    index = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        meta = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        if not meta.get("name"):
            continue
        index.append({
            "name": meta["name"],
            "description": meta.get("description", ""),
            "path": str(skill_dir),
            "size_chars": skill_md.stat().st_size,
        })
    return index


def build_skills_prompt(index: list[dict]) -> str:
    if not index:
        return ""
    lines = [
        "",
        "## 可用技能(Skills)",
        "以下技能默认未加载。如果当前任务与某个技能相关,",
        "先调用 load_skill 工具加载全文,再按技能内容回答:",
    ]
    for s in index:
        lines.append(f"- {s['name']}: {s['description']}")
    return "\n".join(lines)
