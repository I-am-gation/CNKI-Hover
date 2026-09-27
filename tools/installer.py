"""CNKI-Hover 安装程序（用 PyInstaller `--onefile` 打包成单个 exe）。

设计要点：
- **纯标准库**（不引 PySide6），安装程序本体很小，体积几乎全是内嵌的应用程序文件；
- 把打包好的 `dist/CNKI-Hover` 整个目录内嵌为 `app/CNKI-Hover`；
- 安装到 `%LOCALAPPDATA%\\Programs\\CNKI-Hover`（无需管理员权限）；
- 建开始菜单 + 桌面快捷方式（借 Windows 自带的 WScript.Shell，不需要额外依赖）；
- 顺带放一个「卸载」脚本，并写注册表「添加/删除程序」条目。

用法：
    CNKI-Hover-Setup.exe                # 交互式安装（默认目录）
    CNKI-Hover-Setup.exe /S             # 静默安装（不询问，装完直接启动）
环境变量 `CNKI_HOVER_INSTALL_DIR` 可覆盖安装目录（供自动化验收使用）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = "CNKI-Hover"
EXE_NAME = "CNKI-Hover.exe"
_REG_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\CNKI-Hover"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def app_source() -> Path:
    """内嵌的应用程序目录（PyInstaller 解包到 _MEIPASS/app/CNKI-Hover）。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for cand in (base / "app" / APP_NAME, base / "app", base):
        if (cand / EXE_NAME).is_file() or cand.is_dir():
            if (cand / EXE_NAME).is_file():
                return cand
    return base / "app" / APP_NAME


def default_install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Programs" / APP_NAME


def _copy_with_retry(src_file: str, dst_file: Path, tries: int = 4) -> None:
    """单文件拷贝并重试。

    为什么要重试：Windows 上刚落盘的 exe/dll 常被杀毒软件的实时扫描短暂占用，
    直接 copy 会抛 `PermissionError WinError 32`（实测安装过程就撞上过一次）。
    重试几次通常就能过；确实拷不动再抛给上层。
    """
    last = None
    for i in range(tries):
        try:
            shutil.copy2(src_file, dst_file)
            return
        except PermissionError as e:
            last = e
            time.sleep(0.4 * (i + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.2 * (i + 1))
    raise last if last else RuntimeError("拷贝失败：%s" % src_file)


def copy_app(src: Path, dst: Path) -> int:
    """拷贝应用程序；已存在则先整体替换（保证升级干净）。"""
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target = dst if rel == "." else dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            _copy_with_retry(os.path.join(root, f), target / f)
            n += 1
    return n


def _ansi(text: str) -> str:
    """把文本压成系统 ANSI 码页可表示的形态（写 .vbs/.cmd 用，避免乱码）。"""
    try:
        return text.encode("mbcs", errors="replace").decode("mbcs", errors="replace")
    except Exception:
        return text.encode("ascii", errors="replace").decode("ascii")


def _shell_folder(name: str, fallback: Path) -> Path:
    """读 Windows 登记的特殊文件夹**真实路径**（桌面/开始菜单可能被 OneDrive 重定向）。

    为什么必须这样（用户实测反馈"安装后桌面没有快捷方式"）：
    他的机器上 `C:\\Users\\xxx\\Desktop` 与 `C:\\Users\\xxx\\OneDrive\\Desktop` 同时存在，
    猜测路径会把快捷方式写进**不是当前桌面的那个**。注册表 `User Shell Folders` 才是权威。
    """
    try:
        import winreg  # 标准库（仅 Windows）

        base_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer"
        for sub in ("User Shell Folders", "Shell Folders"):
            try:
                k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base_key + "\\" + sub)
                v, typ = winreg.QueryValueEx(k, name)
                winreg.CloseKey(k)
                s = os.path.expandvars(str(v)) if typ == winreg.REG_EXPAND_SZ else str(v)
                if s.strip():
                    return Path(s)
            except FileNotFoundError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _desktop_dir() -> Path:
    base = os.environ.get("USERPROFILE") or str(Path.home())
    return _shell_folder("Desktop", Path(base) / "Desktop")


def _programs_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return _shell_folder("Programs", Path(base) / "Microsoft" / "Windows" /
                         "Start Menu" / "Programs")


def make_shortcut(lnk: Path, target: Path, workdir: Path, desc: str = "") -> bool:
    """建 .lnk：用 Windows 自带的 **WScript.Shell**（cscript 执行临时 .vbs）。

    为什么不用 PowerShell：实测在本机被拦截/执行策略影响而失败；WScript.Shell 更稳。
    """
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
        vbs = Path(tempfile.gettempdir()) / "_cnki_mkshortcut.vbs"
        script = (
            'Set o = CreateObject("WScript.Shell")\n'
            'Set s = o.CreateShortcut("%s")\n'
            's.TargetPath = "%s"\n'
            's.WorkingDirectory = "%s"\n'
            's.Description = "%s"\n'
            's.Save\n' % (_ansi(str(lnk)), _ansi(str(target)),
                          _ansi(str(workdir)), _ansi(desc))
        )
        vbs.write_text(script, encoding="mbcs", errors="replace")
        subprocess.run(["cscript", "//nologo", str(vbs)],
                       capture_output=True, timeout=60)
        try:
            vbs.unlink()
        except Exception:
            pass
        return lnk.is_file()
    except Exception:
        return False


def write_uninstaller(inst: Path) -> None:
    """写卸载脚本 + 注册表「添加/删除程序」条目（用标准库 winreg，不依赖 reg.exe）。"""
    bat = inst / "卸载.cmd"
    try:
        bat.write_text(
            "@echo off\r\n"
            "taskkill /F /IM %s >nul 2>&1\r\n" % EXE_NAME +
            'del /f /q "%s" >nul 2>&1\r\n' % _ansi(str(_desktop_dir() / (APP_NAME + ".lnk"))) +
            'rd /s /q "%s" >nul 2>&1\r\n' % _ansi(str(_programs_dir() / APP_NAME)) +
            'reg delete "%s" /f >nul 2>&1\r\n' % _REG_KEY +
            "timeout /t 1 /nobreak >nul\r\n"
            'cd /d "%%~dp0\\.."\r\n'
            'rd /s /q "%%~dp0"\r\n',
            encoding="mbcs", errors="replace")
    except Exception:
        pass
    try:
        import winreg  # 标准库（仅 Windows）

        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Uninstall\CNKI-Hover",
                                 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, '"%s"' % bat)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(inst))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(inst / EXE_NAME))
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
    except Exception:
        pass


def main() -> int:
    silent = "/S" in [a.upper() for a in sys.argv[1:]]
    env_dir = os.environ.get("CNKI_HOVER_INSTALL_DIR", "").strip()
    inst = Path(env_dir) if env_dir else default_install_dir()

    src = app_source()
    if not (src / EXE_NAME).is_file():
        say("错误：安装程序内嵌的应用程序不完整（缺少 %s）。" % EXE_NAME)
        say("请重新下载安装包。")
        if not silent:
            input("按回车退出…")
        return 1

    say("=" * 54)
    say("  %s 安装程序" % APP_NAME)
    say("=" * 54)
    say("  安装位置：%s" % inst)
    if not silent and not env_dir:
        ans = input("  回车继续，或输入其它路径后回车：").strip().strip('"')
        if ans:
            inst = Path(ans)
            say("  改为：%s" % inst)
    say()

    say("正在安装…")
    t0 = time.perf_counter()
    try:
        n = copy_app(src, inst)
    except Exception as e:
        say("安装失败：%s: %s" % (type(e).__name__, e))
        if not silent:
            input("按回车退出…")
        return 1
    say("  已复制 %d 个文件（%.1fs）" % (n, time.perf_counter() - t0))

    exe = inst / EXE_NAME
    start_menu = Path(os.environ.get("APPDATA") or str(Path.home())) / \
        "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME
    ok_sm = make_shortcut(start_menu / ("%s.lnk" % APP_NAME), exe, inst, "知网悬浮查询器")
    ok_dt = make_shortcut(Path.home() / "Desktop" / ("%s.lnk" % APP_NAME), exe, inst, "知网悬浮查询器")
    write_uninstaller(inst)
    say("  开始菜单快捷方式：%s" % ("已创建" if ok_sm else "创建失败（可手动运行 %s）" % exe))
    say("  桌面快捷方式：%s" % ("已创建" if ok_dt else "未创建"))
    say()
    say("安装完成。首次运行会要求用学校统一身份认证登录一次，")
    say("登录态会加密保存在本机，之后长期免登录。")
    say()

    launch = silent
    if not silent:
        a = input("是否现在启动 %s？[Y/n] " % APP_NAME).strip().lower()
        launch = a in ("", "y", "yes")
    if launch:
        try:
            subprocess.Popen([str(exe)], cwd=str(inst))
            say("已启动。")
        except Exception as e:
            say("启动失败：%s" % e)
    if not silent:
        input("按回车退出…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
