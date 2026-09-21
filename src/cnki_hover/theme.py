"""主题（C5 产出）。

设计：**调色板驱动**，不是把颜色写死在各个控件里。
- `PALETTES` 定义 dark / light 两套语义色（bg / panel / text / subtext / border / accent / …）
- `qss(kind, dark_default)` 返回某控件在**当前主题**下的 QSS；dark 时原样返回传入的默认串
  （保证深色外观与既有验收完全一致），light 时返回对应的浅色版式
- `bind(widget, kind, dark_default)` 一次性完成「套用 + 订阅主题变更」，实现**不重启热切换**
- 同时 `apply_app_palette()` 设置 QApplication 调色板，覆盖 QTextBrowser、消息框等系统控件

公共契约：
    theme.current() -> "dark" | "light"
    theme.set_theme("light")          # 广播 changed，所有 bind 过的控件即时换肤
    theme.bind(widget, "overlay", QSS)
    theme.qss("overlay", QSS) -> str
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPalette

# 语义色
PALETTES = {
    "dark": {
        "bg": "#101216", "panel": "rgba(22, 24, 29, 236)", "panel_solid": "#16181D",
        "raised": "#1F2229", "hover": "rgba(255,255,255,16)", "text": "#EDEFF3",
        "subtext": "#9AA3B2", "border": "rgba(255,255,255,38)", "border_soft": "rgba(255,255,255,22)",
        "accent": "#4C8DFF", "accent_soft": "rgba(76,141,255,52)", "accent_btn": "rgba(76,141,255,60)",
        "err": "#FF8A8A", "warn": "#FFCC66", "ok": "#37C871",
        "scrollbar": "rgba(255,255,255,46)", "disabled_text": "#5A6270",
    },
    "light": {
        "bg": "#F5F6F8", "panel": "rgba(255, 255, 255, 242)", "panel_solid": "#FFFFFF",
        "raised": "#EEF0F4", "hover": "rgba(0,0,0,14)", "text": "#1B1F27",
        "subtext": "#5F6875", "border": "rgba(0,0,0,48)", "border_soft": "rgba(0,0,0,26)",
        "accent": "#1F6FEB", "accent_soft": "rgba(31,111,235,44)", "accent_btn": "rgba(31,111,235,52)",
        "err": "#C0392B", "warn": "#B7791F", "ok": "#1E8E4E",
        "scrollbar": "rgba(0,0,0,60)", "disabled_text": "#A8AEB8",
    },
}

_current = "dark"


class _ThemeBus(QObject):
    changed = Signal(str)


bus = _ThemeBus()


def current() -> str:
    return _current


def palette(name: Optional[str] = None) -> dict:
    return PALETTES.get(name or _current, PALETTES["dark"])


def set_theme(name: str, app=None) -> str:
    """切换主题并广播（所有 bind 过的控件即时换肤，无需重启）。"""
    global _current
    name = "light" if str(name).lower() == "light" else "dark"
    _current = name
    if app is not None:
        apply_app_palette(app, name)
    bus.changed.emit(name)
    return name


def apply_app_palette(app, name: Optional[str] = None) -> None:
    """设置 QApplication 调色板（作用于 QTextBrowser / 菜单 / 消息框等系统控件）。"""
    p = palette(name)
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(p["bg"]))
    pal.setColor(QPalette.WindowText, QColor(p["text"]))
    pal.setColor(QPalette.Base, QColor(p["panel_solid"]))
    pal.setColor(QPalette.AlternateBase, QColor(p["raised"]))
    pal.setColor(QPalette.Text, QColor(p["text"]))
    pal.setColor(QPalette.Button, QColor(p["raised"]))
    pal.setColor(QPalette.ButtonText, QColor(p["text"]))
    pal.setColor(QPalette.Highlight, QColor(p["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ToolTipBase, QColor(p["panel_solid"]))
    pal.setColor(QPalette.ToolTipText, QColor(p["text"]))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(p["disabled_text"]))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(p["disabled_text"]))
    app.setPalette(pal)


# ---------------------------------------------------------------- QSS 生成
def _light_qss(kind: str) -> str:
    p = palette("light")
    if kind == "overlay":
        return f"""
#cnkiOverlay {{ background: {p['panel']}; border: 1px solid {p['border']};
                border-radius: 14px; }}
#cnkiField {{ background: {p['raised']}; border: 1px solid {p['border']};
              border-radius: 7px; color: {p['text']}; font-size: 13px; padding: 4px 6px;
              min-width: 74px; }}
#cnkiField::drop-down {{ border: none; width: 16px; }}
#cnkiField QAbstractItemView {{ background: {p['panel_solid']}; color: {p['text']};
              border: 1px solid {p['border']}; selection-background-color: {p['accent']}; }}
#cnkiInput {{ background: transparent; border: none; color: {p['text']};
              font-size: 17px; padding: 0 4px; selection-background-color: {p['accent']}; }}
#cnkiIcon {{ color: {p['accent']}; font-size: 17px; padding: 0 2px 0 6px; }}
#cnkiStatus {{ color: {p['subtext']}; font-size: 12px; padding: 0 4px; }}
#cnkiHint {{ color: {p['subtext']}; font-size: 12px; padding: 0 10px 0 0; }}
"""
    if kind == "result":
        return f"""
#rlRoot {{ background: transparent; }}
QScrollArea {{ background: transparent; border: none; }}
#rlScrollBody {{ background: transparent; }}
#rlHint {{ color: {p['subtext']}; font-size: 13px; padding: 16px 18px; }}
#rlErr  {{ color: {p['err']}; font-size: 13px; padding: 16px 18px; }}
#rlRow {{ background: transparent; border-radius: 9px; }}
#rlRow:hover {{ background: {p['hover']}; }}
#rlRow[selected="true"] {{ background: {p['accent_soft']}; }}
#rlTitle {{ color: {p['text']}; font-size: 14px; }}
#rlMeta  {{ color: {p['subtext']}; font-size: 12px; }}
#rlBadge {{ color: {p['accent']}; font-size: 11px; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p['scrollbar']}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""
    if kind == "reader":
        return f"""
#rwRoot {{ background: {p['bg']}; }}
#rwHead {{ background: {p['panel_solid']}; border-bottom: 1px solid {p['border']}; }}
#rwTitle {{ color: {p['text']}; font-size: 15px; font-weight: 600; }}
#rwMeta  {{ color: {p['subtext']}; font-size: 12px; }}
#rwTab {{ background: {p['raised']}; border: 1px solid {p['border']}; border-radius: 7px;
          color: {p['subtext']}; padding: 5px 14px; font-size: 12px; }}
#rwTab[active="true"] {{ background: {p['accent_btn']}; color: {p['text']};
                         border-color: {p['accent']}; }}
#rwStatus {{ color: {p['subtext']}; font-size: 12px; padding: 6px 14px;
             background: {p['raised']}; }}
QTextBrowser {{ background: {p['bg']}; border: none; color: {p['text']}; font-size: 15px; }}
"""
    if kind == "pageview":
        return f"""
#pvRoot {{ background: {p['bg']}; }}
#pvBar {{ background: {p['panel_solid']}; border-bottom: 1px solid {p['border']}; }}
#pvBar QLabel {{ color: {p['subtext']}; font-size: 12px; }}
#pvBar QPushButton {{ background: {p['raised']}; border: 1px solid {p['border']};
                      border-radius: 6px; color: {p['text']}; padding: 4px 10px; font-size: 12px; }}
#pvBar QPushButton:hover {{ background: {p['hover']}; }}
#pvBar QPushButton:disabled {{ color: {p['disabled_text']}; }}
#pvSlot {{ background: {p['panel_solid']}; border: 1px solid {p['border_soft']}; }}
#pvSlotText {{ color: {p['disabled_text']}; font-size: 12px; }}
QScrollArea {{ border: none; background: {p['bg']}; }}
#pvBody {{ background: {p['bg']}; }}
"""
    if kind == "login":
        return f"""
QDialog {{ background: {p['panel_solid']}; }}
QLabel  {{ color: {p['text']}; font-size: 13px; }}
QLabel#title {{ font-size: 16px; font-weight: 600; }}
QLabel#sub   {{ color: {p['subtext']}; font-size: 12px; }}
QLabel#err   {{ color: {p['err']}; font-size: 12px; }}
QLineEdit {{ background: {p['raised']}; border: 1px solid {p['border']};
             border-radius: 8px; color: {p['text']}; padding: 7px 10px; font-size: 13px; }}
QLineEdit:focus {{ border: 1px solid {p['accent']}; }}
QPushButton {{ background: {p['accent']}; border: none; border-radius: 8px;
               color: #FFFFFF; padding: 8px 18px; font-size: 13px; }}
QPushButton:disabled {{ background: {p['raised']}; color: {p['disabled_text']}; }}
QPushButton#ghost {{ background: transparent; border: 1px solid {p['border']};
                     color: {p['subtext']}; }}
"""
    if kind == "settings":
        return f"""
#stRoot {{ background: {p['bg']}; }}
QLabel {{ color: {p['text']}; font-size: 13px; }}
QLabel#stTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#stHint  {{ color: {p['subtext']}; font-size: 12px; }}
QLabel#stErr   {{ color: {p['err']}; font-size: 12px; }}
QGroupBox {{ border: 1px solid {p['border_soft']}; border-radius: 10px;
             margin-top: 14px; padding: 12px 12px 8px 12px;
             color: {p['subtext']}; font-size: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
QLineEdit, QComboBox {{ background: {p['raised']}; border: 1px solid {p['border']};
                        border-radius: 8px; color: {p['text']}; padding: 6px 10px; font-size: 13px; }}
QComboBox QAbstractItemView {{ background: {p['panel_solid']}; color: {p['text']};
                               selection-background-color: {p['accent']}; }}
QCheckBox {{ color: {p['text']}; font-size: 13px; spacing: 8px; }}
QPushButton {{ background: {p['raised']}; border: 1px solid {p['border']}; border-radius: 8px;
               color: {p['text']}; padding: 7px 16px; font-size: 13px; }}
QPushButton:hover {{ background: {p['hover']}; }}
QPushButton#primary {{ background: {p['accent']}; border: none; color: #FFFFFF; }}
QPushButton#danger {{ background: transparent; border: 1px solid {p['err']}; color: {p['err']}; }}
"""
    return ""


def qss(kind: str, dark_default: str = "") -> str:
    """取某控件在当前主题下的 QSS。dark 时返回调用方提供的默认串（保证深色零变化）。"""
    if _current == "dark":
        return dark_default
    light = _light_qss(kind)
    return light or dark_default


def bind(widget, kind: str, dark_default: str = "") -> None:
    """套用当前主题并订阅变更（主题热切换时自动重设 stylesheet）。"""
    widget.setStyleSheet(qss(kind, dark_default))
    bus.changed.connect(lambda _n: widget.setStyleSheet(qss(kind, dark_default)))
