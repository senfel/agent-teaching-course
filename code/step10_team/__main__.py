"""__main__.py — 包入口,让 python -m step10_team 能跑

python -m 的执行流程:
  1. Python 把 step10_team 当作包导入(触发 __init__.py)
  2. 自动找到包里的 __main__.py 并执行
  3. __main__.py 调用 main.py 里的 main() 函数

运行方式:
    cd code && python -m step10_team
    # 或
    PYTHONPATH=code python -m step10_team

为什么需要它:
    拆分后 main.py 里有 from .config import ... 等相对导入,
    直接 python main.py 会报错(直接运行文件时 Python 不认包上下文);
    python -m 会先加载包再执行,相对导入才正常。
"""
from .main import main

main()
