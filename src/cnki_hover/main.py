"""应用装配与生命周期（B1 产出）。

职责：
- 创建 QApplication 与单实例锁（Windows 命名互斥体）
- 装配托盘 / 全局热键 / 悬浮窗，并把三者连起来
- 提供 `AppShell` 供自动化验收在进程内驱动（见 tools/verify_shell.py）

命令行：
    python -m cnki_hover.main                     # 正常启动
    python -m cnki_hover.main --check-singleton    # 只探测单实例：0=空闲，2=已被占用
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication

from . import theme
from .cache import get_cache
from .config import Config, load_config
from .hotkey import GlobalHotkey
from .log import get_logger, log_event
from .navigator import Navigator
from .login_ui import LoginController, LoginDialog
from .overlay import Overlay
from .prefetch import Prefetcher
from .reader_window import ReaderWindow
from .result_list import ResultListWidget, SearchController
from .settings import SettingsWindow
from .tray import Tray, make_icon

LOG = get_logger("main")

MUTEX_NAME = "CNKI_Hover_SingleInstance_v1"   # 会话内命名互斥体（Global\ 需特权，故不用）
ERROR_ALREADY_EXISTS = 183

_mutex_handle = None  # 必须常驻，否则句柄被回收、锁失效

# 热键注册失败时按序降级尝试
FALLBACK_HOTKEYS = ("Ctrl+Alt+Space", "Ctrl+Shift+Space", "Alt+`")


def acquire_single_instance() -> bool:
    """尝试获取单实例锁。True=本次是唯一实例；False=已有实例在跑。"""
    global _mutex_handle
    if sys.platform != "win32":
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    h = k32.CreateMutexW(None, False, MUTEX_NAME)
    if not h:
        log_event(LOG, "mutex_create_failed", winerror=ctypes.get_last_error())
        return False
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        try:
            k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            pass
        log_event(LOG, "singleton_blocked")
        return False
    _mutex_handle = h
    log_event(LOG, "singleton_acquired")
    return True


def release_single_instance() -> None:
    global _mutex_handle
    if _mutex_handle and sys.platform == "win32":
        try:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(_mutex_handle)
        except Exception:  # noqa: BLE001
            pass
    _mutex_handle = None


class _OverlayKeyRouter(QObject):
    """把悬浮输入框里的 ↑↓ / Enter 路由到导航状态机。

    - ↑↓：移动高亮（与滚轮、悬停同一状态机）
    - Enter：**有结果时打开当前高亮项**（消费事件，避免又触发一次检索）；
             无结果时才回落到「发起检索」。
    """

    def __init__(self, shell: "AppShell"):
        super().__init__(shell)
        self.shell = shell

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.KeyPress:
            k = event.key()
            if k == Qt.Key_Down:
                self.shell.navigator.move(+1)
                return True
            if k == Qt.Key_Up:
                self.shell.navigator.move(-1)
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if self.shell.result_list.items:
                    self.shell.open_current_item()
                    return True
        return False


class AppShell(QObject):
    """把托盘 / 热键 / 悬浮窗 / 检索 / 结果列表 / 阅读窗 / 设置 装成一个可启停的应用壳。"""

    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, config: Optional[Config] = None, app: Optional[QApplication] = None):
        super().__init__()
        self.config = config or load_config()
        self.app = app
        self._owns_app = app is None
        if self.app is None:
            self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setApplicationName("CNKI-Hover")
        self.app.setQuitOnLastWindowClosed(False)

        # 主题（启动即套用已保存主题）
        try:
            theme.apply_app_palette(self.app, str(self.config.get("theme", "dark")))
            theme.set_theme(str(self.config.get("theme", "dark")))
        except Exception:  # noqa: BLE001
            pass

        self.overlay = Overlay(self.config)
        self.tray = Tray(self.config)
        self.hotkey = GlobalHotkey(str(self.config.get("hotkey", "Alt+Space")))

        # 结果列表 / 导航 / 预取 / 阅读窗 / 设置
        self.result_list = ResultListWidget(self.config)
        self.overlay.attach_result_widget(self.result_list)
        self.navigator = Navigator(self.config)
        self.navigator.attach(self.result_list)
        self.prefetcher = Prefetcher(
            delay_ms=int(self.config.get("prefetch_delay_ms", 1000) or 1000), config=self.config)
        self.prefetcher.enabled = bool(self.config.get("prefetch_enabled", True))
        self.prefetcher.attach(navigator=self.navigator)
        self.search = SearchController(overlay=self.overlay, config=self.config,
                                       result_list=self.result_list)
        self.reader = ReaderWindow(self.config)
        self.settings = SettingsWindow(self.config, hotkey=self.hotkey, tray=self.tray,
                                       cache=get_cache(), app=self.app)

        # 连线
        self.hotkey.activated.connect(self.toggle_overlay)
        self.tray.show_requested.connect(self.show_overlay)
        self.tray.settings_requested.connect(self.open_settings)
        self.tray.quit_requested.connect(self.request_quit)
        self.settings.config_changed.connect(self._on_config_changed)
        self.navigator.activated.connect(self.open_current_item)
        self.reader.closed.connect(self._on_reader_closed)

        # 悬浮窗输入框里的键鼠通道：↑↓ 移动高亮、Enter 打开当前项（否则交给检索）
        self._key_router = _OverlayKeyRouter(self)
        self.overlay.input.installEventFilter(self._key_router)

        self.overlay.set_status("")
        self._started = False

    # ---------------------------------------------------------------- 启停
    def start(self) -> bool:
        if self._started:
            return True
        self.tray.show()
        if not self.hotkey.register():
            for alt in FALLBACK_HOTKEYS:
                if alt == self.hotkey.sequence:
                    continue
                if self.hotkey.update(alt):
                    log_event(LOG, "hotkey_fallback_used", sequence=alt)
                    break
            else:
                self.overlay.set_status("全局热键注册失败，请到「设置」里换一个组合键")
                log_event(LOG, "hotkey_all_failed")
        # 后台预热认证会话：让首次检索不等认证探针（失败不阻塞启动）
        try:
            QTimer.singleShot(200, lambda: self.search.warmup())
        except Exception:  # noqa: BLE001
            pass
        # 首启向导 / 会话过期引导：无可用会话时弹出登录窗（**非模态**，不阻塞托盘与热键）
        try:
            QTimer.singleShot(400, self._first_run_guide)
        except Exception:  # noqa: BLE001
            pass
        self._started = True
        log_event(LOG, "shell_started",
                  hotkey=self.hotkey.sequence, hotkey_ok=self.hotkey.registered)
        return True

    def _first_run_guide(self) -> None:
        """首次运行 / 会话过期时的登录引导（首启向导）。"""
        try:
            self.login_ctl = LoginController(tray=self.tray, config=self.config)
            if self.login_ctl.silent_login():
                log_event(LOG, "guide_skip_logged_in", institution=self.login_ctl.summary().get("institution"))
                return
            dlg = LoginDialog(self.login_ctl)
            self.login_dialog = dlg          # 持引用防被 GC
            dlg.set_credentials(
                (self.config.get("institution", "") or ""), "", "")
            dlg.show()                        # 非模态：不阻塞热键与托盘
            log_event(LOG, "guide_login_shown", state=self.login_ctl.state)
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "guide_failed", error=str(e)[:150])

    def shutdown(self) -> None:
        """干净退出：注销热键、隐藏托盘与悬浮窗、停掉预取与阅读窗。"""
        try:
            self.prefetcher.cancel()
            self.hotkey.unregister()
        except Exception:  # noqa: BLE001
            pass
        for w in (getattr(self, "reader", None), getattr(self, "settings", None)):
            try:
                if w is not None:
                    w.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.overlay.hide_overlay()
            self.overlay.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tray.hide()
        except Exception:  # noqa: BLE001
            pass
        self._started = False
        log_event(LOG, "shell_shutdown")

    def request_quit(self) -> None:
        self.shutdown()
        self.quit_requested.emit()

    # ---------------------------------------------------------------- 交互
    def toggle_overlay(self) -> None:
        self.overlay.toggle()

    def show_overlay(self) -> None:
        self.overlay.show_overlay()

    def open_settings(self) -> None:
        try:
            self.settings.open_window()
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "open_settings_failed", error=str(e)[:120])

    def open_current_item(self) -> None:
        """把当前高亮项交给阅读窗口打开。"""
        item = self.navigator.current_item()
        if item is None:
            return
        log_event(LOG, "open_item", stable_id=getattr(item, "stable_id", ""),
                  title=getattr(item, "title", "")[:40])
        self.reader.open_item(item)

    def _on_reader_closed(self) -> None:
        """阅读窗关闭 → 回到结果列表，并把焦点还给原高亮位。"""
        try:
            self.overlay.show_overlay()
            if 0 <= self.navigator.index < len(self.result_list.rows):
                self.result_list.rows[self.navigator.index].setFocus()
            self.result_list.setFocus()
        except Exception:  # noqa: BLE001
            pass

    def _on_config_changed(self, mapping: dict) -> None:
        try:
            self.prefetcher.enabled = bool(mapping.get("prefetch_enabled", True))
        except Exception:  # noqa: BLE001
            pass

    def on_submit(self, query: str) -> None:
        log_event(LOG, "submit_received", query=query[:60])


def _selfcheck(out_path: str) -> int:
    """打包产物自检：在**冻结环境**里逐项验证主链路是否可用，结果写入 JSON。

    存在的理由：发行版以 --windowed 打包，没有控制台可读；用这个模式可以
    让 D2 的「干净路径解包 → 双击运行 → 主链路可用」变成**可判定**的检查。
    用法：CNKI-Hover.exe --selfcheck <out.json>
    """
    import json
    result = {"ok": True, "steps": []}

    def step(name, ok, detail=""):
        result["steps"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
        if not ok:
            result["ok"] = False

    try:
        from .paths import PROJECT_ROOT
        step("frozen", getattr(sys, "frozen", False), "sys.frozen=%s" % getattr(sys, "frozen", False))
        step("runtime_root", True, str(PROJECT_ROOT))
        step("python", True, sys.version.split()[0])
        try:
            import PySide6  # noqa: F401
            import requests  # noqa: F401
            import cryptography  # noqa: F401
            import pymupdf  # noqa: F401
            import win32api  # noqa: F401
            step("imports", True, "PySide6/requests/cryptography/pymupdf/pywin32 全部可导入")
        except Exception as e:  # noqa: BLE001
            step("imports", False, "%s: %s" % (type(e).__name__, e))

        app = QApplication.instance() or QApplication([])
        cfg = load_config()

        try:
            from cnki_api.auth import get_authenticated_client, session_summary
            client = get_authenticated_client()
            step("session", True, "机构=%s" % session_summary(client).get("institution"))
        except Exception as e:  # noqa: BLE001
            client = None
            step("session", False, "%s: %s" % (type(e).__name__, e))

        if client is not None:
            try:
                from cnki_api import search as S
                res = S.search("空地协同 巡逻", client=client)
                step("search", len(res.items) > 0, "total=%d items=%d" % (res.total, len(res.items)))
                if res.items:
                    from cnki_api import reader as R
                    sid = res.items[0].stable_id
                    # 信息项：缓存是否预热不作为通过条件（首次运行本来就该是冷的）
                    step("cached_text", True,
                         "stable_id=%s cached=%s" % (sid, R.has_cached_text(sid)))
            except Exception as e:  # noqa: BLE001
                step("search", False, "%s: %s" % (type(e).__name__, e))

        try:
            shell = AppShell(config=cfg, app=app)
            step("shell_build", shell.overlay is not None and shell.tray is not None
                 and shell.hotkey is not None and shell.reader is not None and shell.settings is not None,
                 "overlay/tray/hotkey/reader/settings 均已装配")
            shell.shutdown()
        except Exception as e:  # noqa: BLE001
            step("shell_build", False, "%s: %s" % (type(e).__name__, e))
    except Exception as e:  # noqa: BLE001
        step("fatal", False, "%s: %s" % (type(e).__name__, e))

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        return 1
    return 0 if result["ok"] else 1


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    if "--check-singleton" in argv:
        ok = acquire_single_instance()
        print("SINGLETON_FREE" if ok else "ALREADY_RUNNING")
        if ok:
            release_single_instance()
        return 0 if ok else 2

    if "--selfcheck" in argv:
        i = argv.index("--selfcheck")
        out = argv[i + 1] if len(argv) > i + 1 else "selfcheck.json"
        return _selfcheck(out)

    if not acquire_single_instance():
        print("CNKI-Hover 已在运行（单实例限制）。")
        return 2

    config = load_config()
    app = QApplication(argv)
    app.setWindowIcon(make_icon(False))

    shell = AppShell(config=config, app=app)
    shell.quit_requested.connect(app.quit)
    shell.start()

    try:
        code = app.exec()
    finally:
        shell.shutdown()
        release_single_instance()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
