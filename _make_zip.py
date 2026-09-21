"""打包 Windows 发行 zip（dist + 机构示例 + 首次使用说明）。"""
import os
import sys
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "CNKI-Hover-1.0.0-win-x64.zip")
SRC = os.path.join(BASE, "dist", "CNKI-Hover")

EXCLUDE_EXT = {".log"}
EXCLUDE_NAMES = {"_smoke.json", "_smoke_home"}


def main():
    if not os.path.isdir(SRC):
        print("缺少 dist/CNKI-Hover，请先打包")
        return 1
    if os.path.exists(OUT):
        os.remove(OUT)
    n = 0
    total = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_NAMES]
            for f in files:
                if os.path.splitext(f)[1].lower() in EXCLUDE_EXT:
                    continue
                p = os.path.join(root, f)
                rel = os.path.join("CNKI-Hover", os.path.relpath(p, SRC))
                z.write(p, rel)
                n += 1
                total += os.path.getsize(p)
        for extra in ("institutions.example.json", "README-FIRST.txt"):
            p = os.path.join(BASE, extra)
            if os.path.isfile(p):
                z.write(p, extra)
                n += 1
                total += os.path.getsize(p)
    size = os.path.getsize(OUT)
    print("zip 完成：文件 %d 个，原始 %.1f MB -> 压缩后 %.1f MB"
          % (n, total / 1048576.0, size / 1048576.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
