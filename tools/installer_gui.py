"""CNKI-Hover 图形化安装向导（PySide6 QWizard）。

为什么用 PySide6 而不是 tkinter：本机 venv 的 Python **没有 tkinter**
（`import tkinter` 直接 ModuleNotFoundError），而 PySide6 本来就是主程序的依赖，
视觉上也和主程序一致、更像"正常软件的安装体验"（用户实测反馈：不能弹命令行窗口）。

页面流：欢迎（说明 + 安装位置 + 浏览）→ 安装中（进度条）→ 完成（可勾选立即启动）。

静默安装（供自动化验收 / 高级用户）：`CNKI-Hover-Setup.exe --silent`
    或环境变量 `CNKI_HOVER_INSTALL_DIR` 指定安装目录。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QWizard, QWizardPage, QVBoxLayout, QWidget,
)

from installer import (  # 安装引擎（同目录，PyInstaller 会一并打包）
    APP_NAME, EXE_NAME, _copy_with_retry, _desktop_dir, _programs_dir,
    app_source, default_install_dir, make_shortcut, write_uninstaller,
)

ICON_NAME = "app.ico"


def _icon_path() -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for cand in (base / ICON_NAME, base / "assets" / ICON_NAME,
                 Path(__file__).resolve().parent.parent / "assets" / ICON_NAME):
        if cand.is_file():
            return str(cand)
    return ""


def _count_files(src: Path) -> int:
    n = 0
    for _r, _d, files in os.walk(src):
        n += len(files)
    return n


def do_install(inst: Path, progress=None) -> dict:
    """执行安装（拷贝应用 + 快捷方式 + 卸载项）。progress(done,total,name) 供进度条。"""
    src = app_source()
    if not (src / EXE_NAME).is_file():
        raise RuntimeError("安装程序内嵌的应用文件不完整，请重新下载安装包。")
    total = _count_files(src)
    done = 0
    if inst.exists():
        shutil.rmtree(inst, ignore_errors=True)
    inst.mkdir(parents=True, exist_ok=True)
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target = inst if rel == "." else inst / rel
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            _copy_with_retry(os.path.join(root, f), target / f)
            done += 1
            if progress is not None and (done % 5 == 0 or done == total):
                progress(done, total, f)
    exe = inst / EXE_NAME
    ok_sm = make_shortcut(_programs_dir() / (APP_NAME + ".lnk"), exe, inst, "知网悬浮查询器")
    ok_dt = make_shortcut(_desktop_dir() / (APP_NAME + ".lnk"), exe, inst, "知网悬浮查询器")
    write_uninstaller(inst)
    return {"files": done, "total": total, "start_menu": ok_sm,
            "desktop": ok_dt, "exe": exe}


class _InstallWorker(QThread):
    progressed = Signal(int, int, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, inst: Path, parent=None) -> None:
        super().__init__(parent)
        self._inst = inst

    def run(self) -> None:  # noqa: D102
        try:
            res = do_install(self._inst, progress=lambda d, t, n: self.progressed.emit(d, t, n))
            self.finished_ok.emit(res)
        except Exception as e:  # noqa: BLE001
            self.failed.emit("%s: %s" % (type(e).__name__, e))


class _Page(QWizardPage):
    """统一底色/字体的页面基类。"""

    def __init__(self, wizard: "SetupWizard", title: str, subtitle: str = "") -> None:
        super().__init__()
        self._w = wizard
        self.setTitle(title)
        if subtitle:
            self.setSubTitle(subtitle)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(10)
        self.lay = lay


class WelcomePage(_Page):
    def __init__(self, wizard: "SetupWizard") -> None:
        super().__init__(wizard, "欢迎使用 %s 安装向导" % APP_NAME,
                         "知网悬浮查询器 —— 全局热键速查、机构免配置、登录一次长期免登录")
        for line in ("· 全局热键唤出极简搜索条，秒级出结果",
                     "· 机构免配置：登录时直接敲学校名，自动补全（覆盖全国高校）",
                     "· 登录态加密保存在本机，之后长期免登录",
                     "· HTML 论文式阅读 / 原版图像流双模式"):
            lb = QLabel(line)
            lb.setStyleSheet("color:#374151; font-size:13px;")
            self.lay.addWidget(lb)
        self.lay.addSpacing(14)
        row = QHBoxLayout()
        row.addWidget(QLabel("安装位置："))
        self.ed = QLineEdit(str(default_install_dir()))
        self.ed.setMinimumHeight(28)
        row.addWidget(self.ed, 1)
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._browse)
        row.addWidget(btn)
        self.lay.addLayout(row)
        tip = QLabel("无需管理员权限；安装到当前用户目录。")
        tip.setStyleSheet("color:#6B7280; font-size:11px;")
        self.lay.addWidget(tip)
        self.lay.addStretch(1)
        self.setCommitPage(True)          # 「下一步」按钮在此页显示为「安装」

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择安装位置",
                                             str(Path(self.ed.text()).parent))
        if d:
            # Windows 习惯用反斜杠：QFileDialog 返回的是正斜杠，必须规范化
            self.ed.setText(os.path.normpath(os.path.join(d, APP_NAME)))

    def validatePage(self) -> bool:  # noqa: N802
        p = Path(os.path.normpath(self.ed.text().strip().strip('"')))
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, APP_NAME, "无法创建安装目录：%s" % e)
            return False
        self.wizard().inst = p
        return True


class ProgressPage(_Page):
    def __init__(self, wizard: "SetupWizard") -> None:
        super().__init__(wizard, "正在安装…")
        self.pb = QProgressBar()
        self.pb.setMinimumHeight(22)
        self.lay.addWidget(self.pb)
        self.lb = QLabel("准备中…")
        self.lb.setStyleSheet("color:#6B7280; font-size:12px;")
        self.lay.addWidget(self.lb)
        self.lay.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802
        self.wizard().button(QWizard.BackButton).hide()
        self.pb.setValue(0)
        self.lb.setText("准备中…")
        self._worker = _InstallWorker(self.wizard().inst, self)
        self._worker.progressed.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_ok)
        self._worker.failed.connect(self._on_fail)
        self._worker.start()

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.pb.setMaximum(max(1, total))
        self.pb.setValue(done)
        self.lb.setText("正在复制 %s（%d / %d）" % (Path(name).name, done, total))

    def _on_ok(self, res: dict) -> None:
        self.wizard().res = res
        self.pb.setValue(self.pb.maximum())
        self.lb.setText("完成。")
        QTimer.singleShot(250, lambda: self.wizard().next())

    def _on_fail(self, msg: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(self, APP_NAME, "安装失败：%s" % msg)
        self.wizard().button(QWizard.BackButton).show()
        self.wizard().back()


class DonePage(_Page):
    def __init__(self, wizard: "SetupWizard") -> None:
        super().__init__(wizard, "安装完成")
        ok = QLabel("✔  %s 已安装" % APP_NAME)
        ok.setStyleSheet("color:#16A34A; font-size:20px; font-weight:bold;")
        self.lay.addWidget(ok)
        self.lb = QLabel("")
        self.lb.setStyleSheet("color:#374151; font-size:12px;")
        self.lay.addWidget(self.lb)
        self.cb = QCheckBox("立即启动 %s" % APP_NAME)
        self.cb.setChecked(True)
        self.lay.addWidget(self.cb)
        self.lay.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802
        r = getattr(self.wizard(), "res", {})
        self.lb.setText("安装位置：%s\n文件 %d 个\n开始菜单快捷方式：%s\n桌面快捷方式：%s"
                        % (self.wizard().inst, r.get("files", 0),
                           "已创建" if r.get("start_menu") else "未创建",
                           "已创建" if r.get("desktop") else "未创建"))

    def validatePage(self) -> bool:  # noqa: N802
        if self.cb.isChecked():
            try:
                subprocess.Popen([str(self.wizard().inst / EXE_NAME)], cwd=str(self.wizard().inst))
            except Exception as e:  # noqa: BLE001
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.critical(self, APP_NAME, "启动失败：%s" % e)
        return True


class SetupWizard(QWizard):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("%s 安装向导" % APP_NAME)
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setButtonText(QWizard.NextButton, "下一步 >")
        self.setButtonText(QWizard.CommitButton, "安装")
        self.setButtonText(QWizard.FinishButton, "完成")
        self.setButtonText(QWizard.CancelButton, "取消")
        ico = _icon_path()
        if ico:
            self.setWindowIcon(QIcon(ico))
            self.setPixmap(QWizard.LogoPixmap, QPixmap(ico))
        self.inst = default_install_dir()
        self.res: dict = {}
        self.addPage(WelcomePage(self))
        self.addPage(ProgressPage(self))
        self.addPage(DonePage(self))
        self.resize(660, 440)


def run_silent() -> int:
    """静默安装（供自动化验收）。"""
    inst = Path(os.environ.get("CNKI_HOVER_INSTALL_DIR", "").strip() or default_install_dir())
    res = do_install(inst)
    print("installed:", res["files"], "files ->", inst,
          "| start_menu=", res["start_menu"], "| desktop=", res["desktop"])
    return 0


def main() -> int:
    argv = [a.lower() for a in sys.argv[1:]]
    if "--silent" in argv or "/s" in argv:
        return run_silent()
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    w = SetupWizard()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
