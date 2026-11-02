"""__main__.py — 包入口,让 python -m step15_deploy 能跑(第 15 期:分发器)

python -m 的执行流程:
  1. Python 把 step15_deploy 当作包导入(触发 __init__.py)
  2. 自动找到包里的 __main__.py 并执行
  3. __main__.py 分发:默认启动 Web 服务,--cli 启动命令行交互

运行方式:
    cd code && python -m step15_deploy          # 默认:Web 服务(FastAPI + 流式响应)
    cd code && python -m step15_deploy --cli    # CLI:命令行交互(第 14 期入口)

为什么需要它:
    拆分后各模块里有 from .config import ... 等相对导入,
    直接 python xxx.py 会报错(直接运行文件时 Python 不认包上下文);
    python -m 会先加载包再执行,相对导入才正常。
"""
import sys


def main():
    if "--cli" in sys.argv:
        from .main import main as cli_main
        cli_main()
    else:
        from .web import run_web
        run_web()


if __name__ == "__main__":
    main()
