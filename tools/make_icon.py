"""生成应用图标（assets/app.ico）。

复用托盘图标同一套绘制逻辑（深色圆角底 + 强调色放大镜），
保证托盘、任务栏、安装包图标视觉一致。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cnki_hover.tray import make_icon  # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    out_dir = os.path.join(ROOT, "assets")
    os.makedirs(out_dir, exist_ok=True)
    png_path = os.path.join(out_dir, "app_256.png")
    ico_path = os.path.join(out_dir, "app.ico")

    icon: QIcon = make_icon(logged_in=False, size=256)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    pngs = []
    for s in sizes:
        p = os.path.join(out_dir, "_s%d.png" % s)
        icon.pixmap(s, s).save(p, "PNG")
        pngs.append((s, p))
    icon.pixmap(256, 256).save(png_path, "PNG")

    try:
        from PIL import Image
        imgs = [Image.open(p).convert("RGBA") for _s, p in pngs]
        base = imgs[-1]
        base.save(ico_path, format="ICO",
                  sizes=[(s, s) for s, _p in pngs])
        print("OK ->", ico_path, os.path.getsize(ico_path), "bytes")
    except Exception as e:  # noqa: BLE001
        print("PIL 生成 ico 失败：", e)
        return 1
    finally:
        for _s, p in pngs:
            try:
                os.remove(p)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
