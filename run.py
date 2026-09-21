"""打包入口脚本（PyInstaller 用）。

为什么需要它：`src/cnki_hover/main.py` 内部使用相对导入（`from .config import ...`），
直接把它当脚本入口会被当成顶层模块，相对导入失败。这里先补 sys.path，再以**包**的形式导入。
开发态也可直接用它启动：`python run.py`
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from cnki_hover.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
