"""系统托盘（常驻层）。

B1 产出。右键菜单：显示主窗 / 设置 / 退出；tooltip 反映登录状态。
图标用 QPainter 程序化生成，免外部资源文件。

公共契约：
    tray = Tray(config)
    tray.set_login_state(logged_in: bool, detail: str = "")
    tray.show_requested / settings_requested / quit_requested : Signal
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from .config import Config, load_config
from .log import get_logger, log_event

LOG = get_logger("tray")

APP_NAME = "知网悬浮查询器"
ACCENT = "#4C8DFF"
BG = "#16181D"


def make_icon(logged_in: bool = False, size: int = 64) -> QIcon:
    """程序化生成托盘图标：深色圆角底 + 强调色放大镜；已登录时右下角点绿点。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    path = QPainterPath()
    path.addRoundedRect(QRectF(2, 2, size - 4, size - 4), size * 0.24, size * 0.24)
    p.fillPath(path, QColor(BG))
    p.setPen(QColor(255, 255, 255, 46))
    p.drawPath(path)

    pen_w = max(3.0, size * 0.085)
    p.setPen(QColor(ACCENT))
    from PySide6.QtGui import QPen
    p.setPen(QPen(QColor(ACCENT), pen_w, Qt.SolidLine, Qt.RoundCap))
    r = size * 0.21
    cx, cy = size * 0.44, size * 0.42
    p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
    p.drawLine(int(cx + r * 0.72), int(cy + r * 0.72),
               int(size * 0.74), int(size * 0.74))

    if logged_in:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#37C871"))
        d = size * 0.20
        p.drawEllipse(QRectF(size - d - 3, size - d - 3, d, d))

    p.end()
    return QIcon(pm)


class Tray(QSystemTrayIcon):
    show_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, config: Optional[Config] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config or load_config()
        self._logged_in = False
        self._detail = ""
        self._menu = QMenu()

        self.act_show = QAction("显示主窗", self)
        self.act_show.triggered.connect(self.show_requested.emit)
        self.act_settings = QAction("设置", self)
        self.act_settings.triggered.connect(self.settings_requested.emit)
        self.act_quit = QAction("退出", self)
        self.act_quit.triggered.connect(self.quit_requested.emit)

        self._menu.addAction(self.act_show)
        self._menu.addAction(self.act_settings)
        self._menu.addSeparator()
        self._menu.addAction(self.act_quit)

        self.setContextMenu(self._menu)
        self.setIcon(make_icon(False))
        self.activated.connect(self._on_activated)
        self.refresh_tooltip()

    # ---------------------------------------------------------------- 状态
    def set_login_state(self, logged_in: bool, detail: str = "") -> None:
        self._logged_in = bool(logged_in)
        self._detail = detail or ""
        self.setIcon(make_icon(self._logged_in))
        self.refresh_tooltip()
        log_event(LOG, "tray_login_state", logged_in=self._logged_in, detail=self._detail[:40])

    def refresh_tooltip(self) -> None:
        if self._logged_in:
            tip = "%s · 已登录%s" % (APP_NAME, ("（%s）" % self._detail) if self._detail else "")
        elif self._detail:
            # 未登录但有说明（如「会话已过期，请重新登录」/「尚未登录」）→ 必须透出给用户
            tip = "%s · %s" % (APP_NAME, self._detail)
        else:
            tip = "%s · 未登录" % APP_NAME
        self.setToolTip(tip)

    @property
    def tooltip(self) -> str:
        return self.toolTip()

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    # ---------------------------------------------------------------- 交互
    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_requested.emit()

    def popup_menu(self) -> None:
        """供自动化验收检查菜单项。"""
        return self._menu
