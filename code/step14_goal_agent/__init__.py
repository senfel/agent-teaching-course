"""__init__.py — 声明 step14_goal_agent/ 是一个 Python 包

有了这个文件(可以为空),Python 才允许包内的模块用相对导入互相引用,
比如 model_router.py 里的 from .config import client、
tools.py 里的 from .hooks import list_hooks、
goal.py 里的 from .agent import chat_with_tools。

删掉它 → 所有 from .xxx import ... 全部报错。
"""
