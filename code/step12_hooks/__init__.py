"""__init__.py — 声明 step12_hooks/ 是一个 Python 包

有了这个文件(可以为空),Python 才允许包内的模块用相对导入互相引用,
比如 hooks.py 里的 from .config import AUDIT_LOG_PATH、
tools.py 里的 from .hooks import list_hooks。

删掉它 → 所有 from .xxx import ... 全部报错。
"""
