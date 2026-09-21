"""设置与系统集成（C5 产出）。

- 设置窗：**热键自定义 / 默认检索项 / 匹配模式 / 主题 / 缓存管理 / 开机自启**
- **配置热更新**：保存后不重启即生效（热键换绑、主题换肤、缓存上限即时生效）
- 托盘菜单入口接入（见 main.AppShell）

公共契约：
    w = SettingsWindow(config, hotkey=..., tray=..., cache=...)
    w.open_window()                     # 载入当前配置并显示
    w.apply() -> bool                   # 校验 + 落盘 + 热更新；失败返回 False 并显示原因
    w.hotkey_changed: Signal[str]
    w.config_changed: Signal[dict]
    Autostart.set_enabled(bool) / is_enabled() / command()
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from . import theme
from .cache import LocalCache, get_cache
from .config import Config, load_config
from .hotkey import HotkeyError, parse_hotkey
from .log import get_logger, log_event

LOG = get_logger("settings")

FIELD_CHOICES = [("主题", "SU"), ("篇名", "TI"), ("关键词", "KY"), ("摘要", "AB"),
                 ("作者", "AU"), ("第一作者", "FI"), ("作者单位", "AF"), ("文献来源", "LY")]

QSS = """
#stRoot { background: #101216; }
QLabel { color: #EDEFF3; font-size: 13px; }
QLabel#stTitle { font-size: 16px; font-weight: 600; }
QLabel#stHint  { color: #9AA3B2; font-size: 12px; }
QLabel#stErr   { color: #FF8A8A; font-size: 12px; }
QGroupBox { border: 1px solid rgba(255,255,255,22); border-radius: 10px;
            margin-top: 14px; padding: 12px 12px 8px 12px;
            color: #9AA3B2; font-size: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QLineEdit, QComboBox, QSpinBox { background: #1F2229; border: 1px solid rgba(255,255,255,38);
            border-radius: 8px; color: #EDEFF3; padding: 6px 10px; font-size: 13px; }
QComboBox QAbstractItemView { background: #16181D; color: #EDEFF3;
            selection-background-color: #4C8DFF; }
QCheckBox { color: #EDEFF3; font-size: 13px; spacing: 8px; }
QPushButton { background: #1F2229; border: 1px solid rgba(255,255,255,38); border-radius: 8px;
            color: #EDEFF3; padding: 7px 16px; font-size: 13px; }
QPushButton:hover { background: #262A33; }
QPushButton#primary { background: #4C8DFF; border: none; color: #FFFFFF; }
QPushButton#danger { background: transparent; border: 1px solid #FF8A8A; color: #FF8A8A; }
"""


class Autostart:
    """Windows 开机自启（HKCU\\...\\Run）。"""

    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    VALUE_NAME = "CNKI-Hover"

    @staticmethod
    def command() -> str:
        """启动命令。打包后直接用 exe；开发态用 pythonw + 显式 sys.path 引导。"""
        exe = Path(sys.executable)
        if getattr(sys, "frozen", False):
            return '"%s"' % exe
        pyw = exe.with_name("pythonw.exe")
        launcher = pyw if pyw.exists() else exe
        src = Path(__file__).resolve().parents[1]      # <项目>/src
        boot = ("import sys;sys.path.insert(0,r'%s');"
                "from cnki_hover.main import main;raise SystemExit(main())" % src)
        return '"%s" -c "%s"' % (launcher, boot)

    @classmethod
    def is_enabled(cls) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import winreg  # noqa: PLC0415
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY) as k:
                val, _t = winreg.QueryValueEx(k, cls.VALUE_NAME)
                return bool(val)
        except FileNotFoundError:
            return False
        except OSError:
            return False

    @classmethod
    def current_value(cls) -> str:
        if sys.platform != "win32":
            return ""
        try:
            import winreg  # noqa: PLC0415
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY) as k:
                return winreg.QueryValueEx(k, cls.VALUE_NAME)[0]
        except OSError:
            return ""

    @classmethod
    def set_enabled(cls, on: bool) -> tuple:
        """写入 / 移除启动项。返回 (成功?, 说明)。"""
        if sys.platform != "win32":
            return False, "仅 Windows 支持开机自启"
        try:
            import winreg  # noqa: PLC0415
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                if on:
                    cmd = cls.command()
                    winreg.SetValueEx(k, cls.VALUE_NAME, 0, winreg.REG_SZ, cmd)
                    log_event(LOG, "autostart_on", cmd=cmd[:120])
                    return True, "已写入启动项"
                try:
                    winreg.DeleteValue(k, cls.VALUE_NAME)
                    log_event(LOG, "autostart_off")
                except FileNotFoundError:
                    pass
                return True, "已移除启动项"
        except OSError as e:
            log_event(LOG, "autostart_failed", error=str(e)[:120])
            return False, "注册表操作失败：%s" % e


class SettingsWindow(QWidget):
    hotkey_changed = Signal(str)
    config_changed = Signal(dict)

    def __init__(self, config: Optional[Config] = None, hotkey=None, tray=None,
                 cache: Optional[LocalCache] = None, app=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config or load_config()
        self.hotkey = hotkey
        self.tray = tray
        self.cache = cache or get_cache()
        self.app = app
        self._built = False

        self.setObjectName("stRoot")
        self.setWindowTitle("知网悬浮查询器 · 设置")
        self.setMinimumWidth(560)
        theme.bind(self, "settings", QSS)
        self._build()
        self.load_from_config()

    # ---------------------------------------------------------------- UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(8)

        t = QLabel("设置")
        t.setObjectName("stTitle")
        root.addWidget(t)
        hint = QLabel("所有改动**保存后立即生效，无需重启**。")
        hint.setObjectName("stHint")
        root.addWidget(hint)

        # —— 交互
        g1 = QGroupBox("交互")
        f1 = QFormLayout(g1)
        f1.setSpacing(8)

        self.ed_hotkey = QLineEdit()
        self.ed_hotkey.setPlaceholderText("如 Alt+Space / Ctrl+Shift+K")
        self.ed_hotkey.textChanged.connect(self._validate_hotkey_live)
        f1.addRow("全局热键", self.ed_hotkey)

        self.cb_field = QComboBox()
        for label, code in FIELD_CHOICES:
            self.cb_field.addItem(label, code)
        f1.addRow("默认检索项", self.cb_field)

        self.cb_match = QComboBox()
        self.cb_match.addItem("模糊", "fuzzy")
        self.cb_match.addItem("精确", "exact")
        f1.addRow("默认匹配", self.cb_match)

        self.cb_theme = QComboBox()
        self.cb_theme.addItem("深色", "dark")
        self.cb_theme.addItem("浅色", "light")
        f1.addRow("主题", self.cb_theme)

        self.ck_prefetch = QCheckBox("结果高亮停留后预取文献（推荐）")
        f1.addRow("", self.ck_prefetch)
        root.addWidget(g1)

        # —— 系统
        g2 = QGroupBox("系统集成")
        f2 = QFormLayout(g2)
        self.ck_autostart = QCheckBox("开机自动启动（写入当前用户启动项）")
        f2.addRow("", self.ck_autostart)
        self.lb_autostart = QLabel("")
        self.lb_autostart.setObjectName("stHint")
        f2.addRow("", self.lb_autostart)
        root.addWidget(g2)

        # —— 缓存
        g3 = QGroupBox("缓存")
        f3 = QFormLayout(g3)
        self.sp_cache = QSpinBox()
        self.sp_cache.setRange(50, 20000)
        self.sp_cache.setSingleStep(50)
        self.sp_cache.setSuffix(" MB")
        f3.addRow("容量上限", self.sp_cache)
        self.lb_cache = QLabel("")
        self.lb_cache.setObjectName("stHint")
        self.lb_cache.setWordWrap(True)
        f3.addRow("当前占用", self.lb_cache)
        row = QHBoxLayout()
        self.btn_cache_info = QPushButton("刷新统计")
        self.btn_cache_purge = QPushButton("清空缓存")
        self.btn_cache_purge.setObjectName("danger")
        self.btn_cache_info.clicked.connect(self.refresh_cache_stats)
        self.btn_cache_purge.clicked.connect(self.purge_cache)
        row.addWidget(self.btn_cache_info)
        row.addWidget(self.btn_cache_purge)
        row.addStretch(1)
        f3.addRow("", row)
        warn = QLabel("提示：清空会同时删掉已下载的原版 PDF，下次阅读需重新向知网取回"
                      "（会占用一次机构下载额度）。")
        warn.setObjectName("stHint")
        warn.setWordWrap(True)
        f3.addRow("", warn)
        root.addWidget(g3)

        self.lb_err = QLabel("")
        self.lb_err.setObjectName("stErr")
        self.lb_err.setWordWrap(True)
        root.addWidget(self.lb_err)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_close = QPushButton("关闭")
        self.btn_apply = QPushButton("保存并应用")
        self.btn_apply.setObjectName("primary")
        self.btn_close.clicked.connect(self.hide)
        self.btn_apply.clicked.connect(self.apply)
        btns.addWidget(self.btn_close)
        btns.addWidget(self.btn_apply)
        root.addLayout(btns)
        self._built = True

    # ---------------------------------------------------------------- 载入
    def load_from_config(self) -> None:
        cfg = self.config
        self.ed_hotkey.setText(str(cfg.get("hotkey", "Alt+Space")))
        code = str(cfg.get("default_field", "SU"))
        idx = next((i for i in range(self.cb_field.count())
                    if self.cb_field.itemData(i) == code), 0)
        self.cb_field.setCurrentIndex(idx)
        m = str(cfg.get("match_mode", "fuzzy"))
        self.cb_match.setCurrentIndex(1 if m == "exact" else 0)
        th = theme.current() or str(cfg.get("theme", "dark"))
        self.cb_theme.setCurrentIndex(1 if th == "light" else 0)
        self.ck_prefetch.setChecked(bool(cfg.get("prefetch_enabled", True)))
        self.sp_cache.setValue(int(cfg.get("cache_max_mb", 500) or 500))
        on = Autostart.is_enabled()
        self.ck_autostart.setChecked(on)
        self.lb_autostart.setText("启动项：%s" % (Autostart.current_value()[:90] or "（未设置）"))
        self.refresh_cache_stats()
        self.lb_err.setText("")

    def refresh_cache_stats(self) -> None:
        try:
            st = self.cache.stats()
            self.lb_cache.setText(
                "共 %d 个文件 / %.1f MB（上限 %.0f MB，占用 %.1f%%）"
                % (st["entries"], st["total_bytes"] / 1048576,
                   st["limit_bytes"] / 1048576, st["usage_ratio"] * 100))
        except Exception as e:  # noqa: BLE001
            self.lb_cache.setText("统计失败：%s" % e)

    # ---------------------------------------------------------------- 校验
    def _validate_hotkey_live(self, text: str) -> None:
        try:
            parse_hotkey(text)
        except HotkeyError as e:
            self.ed_hotkey.setToolTip(str(e))
            if self._built:
                self.lb_err.setText("热键格式：%s" % e)
        else:
            self.ed_hotkey.setToolTip("格式有效")
            if self._built and self.lb_err.text().startswith("热键格式"):
                self.lb_err.setText("")

    # ---------------------------------------------------------------- 应用
    def apply(self) -> bool:
        self.lb_err.setText("")
        new_hotkey = self.ed_hotkey.text().strip()

        # 1) 热键：先校验格式
        try:
            parse_hotkey(new_hotkey)
        except HotkeyError as e:
            self.lb_err.setText("热键无效：%s" % e)
            return False

        # 2) 热键热更新（旧键立即失效、新键立即生效）
        old_hotkey = str(self.config.get("hotkey", ""))
        if self.hotkey is not None and new_hotkey != old_hotkey:
            if not self.hotkey.update(new_hotkey):
                self.lb_err.setText("热键「%s」注册失败（可能被其他程序占用），已保留原热键「%s」。"
                                    % (new_hotkey, self.hotkey.sequence))
                self.ed_hotkey.setText(self.hotkey.sequence)
                return False
            self.hotkey_changed.emit(self.hotkey.sequence)
            log_event(LOG, "hotkey_hot_updated", old=old_hotkey, new=self.hotkey.sequence)

        # 3) 落盘其它设置
        self.config.update({
            "hotkey": self.hotkey.sequence if self.hotkey is not None else new_hotkey,
            "default_field": self.cb_field.currentData(),
            "match_mode": self.cb_match.currentData(),
            "theme": self.cb_theme.currentData(),
            "prefetch_enabled": self.ck_prefetch.isChecked(),
            "cache_max_mb": int(self.sp_cache.value()),
            "autostart": self.ck_autostart.isChecked(),
        })
        try:
            self.config.save()
        except Exception as e:  # noqa: BLE001
            self.lb_err.setText("配置保存失败：%s" % e)
            return False

        # 4) 主题热切换（不重启）
        theme.set_theme(self.cb_theme.currentData(), app=self.app)

        # 5) 缓存上限即时生效
        try:
            self.cache.max_bytes = int(self.sp_cache.value()) * 1024 * 1024
            self.cache.enforce_limit()
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "cache_limit_failed", error=str(e)[:100])

        # 6) 开机自启
        want = self.ck_autostart.isChecked()
        if want != Autostart.is_enabled():
            ok, msg = Autostart.set_enabled(want)
            if not ok:
                self.lb_err.setText(msg)
            self.lb_autostart.setText("启动项：%s" % (Autostart.current_value()[:90] or "（未设置）"))
        else:
            self.lb_autostart.setText("启动项：%s" % (Autostart.current_value()[:90] or "（未设置）"))

        self.refresh_cache_stats()
        self.lb_err.setText("已保存并生效。")
        log_event(LOG, "settings_applied",
                  hotkey=self.hotkey.sequence if self.hotkey else new_hotkey,
                  theme=self.cb_theme.currentData(), autostart=want)
        self.config_changed.emit(self.config.as_dict())
        return True

    def purge_cache(self) -> None:
        freed = self.cache.purge()
        self.refresh_cache_stats()
        self.lb_err.setText("已清空缓存，释放 %.1f MB。" % (freed / 1048576))

    # ---------------------------------------------------------------- 显示
    def open_window(self) -> None:
        self.load_from_config()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):  # noqa: N802
        event.ignore()
        self.hide()
