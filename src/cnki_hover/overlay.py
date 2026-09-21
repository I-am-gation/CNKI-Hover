"""悬浮搜索窗（Ueli 气质：极简、无边框、置顶、失焦即隐、召之即来）。

B1 只落「壳」：搜索条 + 显隐/定位/键鼠入口 + 结果区挂载点。
结果列表由 B3（result_list.py）通过 `attach_result_widget()` 注入，导航由 C1 接管。

公共契约（下游 B3/C1/C2/C5 依赖）：
    overlay = Overlay(config)
    overlay.attach_result_widget(w)   # 把结果控件挂到搜索条下方
    overlay.show_overlay() / hide_overlay() / toggle()
    overlay.submitted: Signal[str]    # 用户按回车/点击「搜索」
    overlay.hidden:    Signal
    overlay.current_query() -> str
    overlay.set_status(text)          # 状态行（登录态/错误提示）
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPoint, QPropertyAnimation, Qt, QTimer, Signal,
)
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

from . import theme
from .config import Config, load_config
from .log import get_logger, log_event

LOG = get_logger("overlay")

# ---- 视觉常量（深色半透明 + 圆角 + 细边框 + 单一强调色） ----
# 面板不透明度：早期 236（92%）。但实测在背后有亮色窗口（编辑器/浏览器）时，
# 文字会明显透出来，用户看成"搜索结果乱了"。提高到 250（98%）—— 视觉上仍有
# 深色半透明质感，但底层文字不再干扰阅读。
BG = "rgba(22, 24, 29, 250)"
BORDER = "rgba(255, 255, 255, 38)"
ACCENT = "#4C8DFF"
TEXT = "#EDEFF3"
SUBTLE = "#9AA3B2"
SUBTEXT = "#9AA3B2"

OVERLAY_WIDTH = 760          # 规格 720–860
OVERLAY_TOP_RATIO = 0.18     # 屏幕中上部
BAR_HEIGHT = 58
RADIUS = 14
ANIM_MS = 90                 # ≤120ms

# 检索项下拉框（对齐知网：主题 / 篇名 / 关键词 / 摘要 / 作者 / …）
FIELD_CHOICES = [("主题", "SU"), ("篇名", "TI"), ("关键词", "KY"), ("摘要", "AB"),
                 ("作者", "AU"), ("第一作者", "FI"), ("作者单位", "AF"), ("文献来源", "LY")]

QSS = f"""
#cnkiOverlay {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
#cnkiField {{
    background: rgba(255,255,255,20);
    border: 1px solid {BORDER};
    border-radius: 7px;
    color: {TEXT};
    font-size: 13px;
    padding: 4px 4px 4px 8px;
    min-width: 78px;
}}
#cnkiField::drop-down {{ border: none; width: 18px; }}
#cnkiField::down-arrow {{
    width: 0; height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid {SUBTLE};
    margin-right: 3px;
}}
#cnkiField QAbstractItemView {{
    background: #1B1E24; color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    outline: none;
}}
#cnkiInput {{
    background: transparent;
    border: none;
    color: {TEXT};
    font-size: 17px;
    padding: 0 4px;
    selection-background-color: {ACCENT};
}}
#cnkiIcon {{
    color: {ACCENT};
    font-size: 17px;
    padding: 0 2px 0 6px;
}}
#cnkiStatus {{
    color: {SUBTEXT};
    font-size: 12px;
    padding: 0 4px;
}}
#cnkiHint {{
    color: {SUBTEXT};
    font-size: 12px;
    padding: 0 10px 0 0;
}}
"""


class Overlay(QWidget):
    submitted = Signal(str)
    hidden = Signal()
    field_changed = Signal(str)          # 检索项切换 → 重新检索

    def __init__(self, config: Optional[Config] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config or load_config()
        self._auto_hide = True
        self._result_widget: Optional[QWidget] = None
        self._anim: Optional[QPropertyAnimation] = None

        self.setWindowTitle("知网悬浮查询器")
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        theme.bind(self, "overlay", QSS)

        self._build_ui()
        self.resize(self._width(), self.sizeHint().height())
        # 检索项初值取配置（设置里可改默认检索项）
        try:
            self.set_field(str(self.config.get("default_field", "SU")))
        except Exception:  # noqa: BLE001
            pass
        self.cb_field.currentIndexChanged.connect(
            lambda _i: self.field_changed.emit(self.current_field()))

    # ---------------------------------------------------------------- UI
    def _width(self) -> int:
        try:
            w = int(self.config.get("overlay_width", OVERLAY_WIDTH) or OVERLAY_WIDTH)
        except Exception:
            w = OVERLAY_WIDTH
        return max(720, min(860, w))

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.panel = QFrame(self)
        self.panel.setObjectName("cnkiOverlay")
        root.addWidget(self.panel)

        pv = QVBoxLayout(self.panel)
        pv.setContentsMargins(14, 8, 12, 8)
        pv.setSpacing(0)

        # 搜索条
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)

        self.icon = QLabel("⌕")
        self.icon.setObjectName("cnkiIcon")
        bar.addWidget(self.icon)

        # 检索项下拉框（用户要求：像知网一样选，不要手打「作者:」）
        self.cb_field = QComboBox()
        self.cb_field.setObjectName("cnkiField")
        for label, code in FIELD_CHOICES:
            self.cb_field.addItem(label, code)
        self.cb_field.setFocusPolicy(Qt.StrongFocus)
        self.cb_field.setToolTip("检索项（对应知网高级检索字段）")
        bar.addWidget(self.cb_field)

        self.input = QLineEdit()
        self.input.setObjectName("cnkiInput")
        self.input.setPlaceholderText("搜索知网…（主题模糊检索；支持 作者: / 篇名: 前缀）")
        self.input.setMinimumHeight(BAR_HEIGHT - 16)
        self.input.returnPressed.connect(self._on_submit)
        self.input.installEventFilter(self)   # Esc 在输入框里也要能收起
        bar.addWidget(self.input, 1)

        self.hint = QLabel("Enter 打开 · Esc 收起 · ↑↓/滚轮 选择")
        self.hint.setObjectName("cnkiHint")
        bar.addWidget(self.hint)

        pv.addLayout(bar)

        self.status = QLabel("")
        self.status.setObjectName("cnkiStatus")
        self.status.setVisible(False)
        pv.addWidget(self.status)

        # 结果区挂载点（B3 注入）；B1 阶段为空
        self.body = QWidget(self.panel)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(0)
        self.body.setVisible(False)
        pv.addWidget(self.body)

    # ---------------------------------------------------------------- 结果区
    def attach_result_widget(self, widget: QWidget) -> None:
        """把结果控件挂到搜索条下方（B3 调用）。"""
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        self._result_widget = widget
        self.body_layout.addWidget(widget)
        widget.setVisible(True)
        self.body.setVisible(True)
        # 结果异步到达后列表高度会变 → 必须让悬浮窗**重新量高**，
        # 否则窗口停在"空列表"时的高度，用户只能看到第一行（其余被裁掉）。
        sig = getattr(widget, "layout_changed", None)
        if sig is not None:
            try:
                sig.connect(self._on_result_layout_changed)
            except Exception:  # noqa: BLE001
                pass
        log_event(LOG, "result_widget_attached", cls=type(widget).__name__)

    def _on_result_layout_changed(self) -> None:
        self.body.setVisible(True)
        # 延到下一轮事件循环再量高：`layout_changed` 是在 set_items 里发出的，
        # 此刻父级布局尚未重算，直接读 sizeHint() 会拿到**过期高度**（实测窗口停在 430px）。
        QTimer.singleShot(0, self._resize_to_content)

    def set_body_visible(self, visible: bool) -> None:
        self.body.setVisible(bool(visible))
        self._resize_to_content()

    def _resize_to_content(self) -> None:
        lay = self.layout()
        if lay is not None:
            lay.activate()
        h = self.sizeHint().height()
        if h <= 0:
            h = self.height()
        new_h = max(self.minimumSizeHint().height(), h)
        changed = (new_h != self.height())
        self.resize(self._width(), new_h)
        if changed:
            # ⚠️ 必须强制重绘：这是 WA_TranslucentBackground 的无边框窗口，
            # 尺寸变化后新暴露的区域不会自动重绘，会残留底层窗口的画面
            # （用户两次反馈"搜索结果乱了"，其实是背后编辑器的文字透出来）。
            for w in (self.panel, self.body, self.input, self.cb_field,
                      getattr(self, "_result_widget", None)):
                try:
                    if w is not None:
                        w.update()
                except Exception:  # noqa: BLE001
                    pass
            self.update()
            self.repaint()
        self._resize_to_content_hook()

    def _resize_to_content_hook(self) -> None:
        """测试钩子：记录最近一次量高结果（供自动化断言）。"""
        self.last_fit_height = self.height()
        self.last_fit_width = self.width()

    def _on_submit(self) -> None:
        q = self.current_query()
        log_event(LOG, "overlay_submit", query=q[:60])
        self.submitted.emit(q)

    # ---------------------------------------------------------------- 状态行
    def set_status(self, text: str, visible: Optional[bool] = None) -> None:
        self.status.setText(text or "")
        self.status.setVisible(bool(text) if visible is None else bool(visible))

    # ---------------------------------------------------------------- 检索项
    def current_field(self) -> str:
        """当前选中的检索项字段码（默认取配置 default_field）。"""
        d = self.cb_field.currentData()
        return str(d or "SU")

    def set_field(self, code: str, block_signals: bool = True) -> None:
        """切换检索项（供前缀解析回显 / 设置同步使用）。"""
        idx = self.cb_field.findData(code)
        if idx < 0 or idx == self.cb_field.currentIndex():
            return
        if block_signals:
            self.cb_field.blockSignals(True)
        self.cb_field.setCurrentIndex(idx)
        if block_signals:
            self.cb_field.blockSignals(False)

    # ---------------------------------------------------------------- 显隐
    def current_query(self) -> str:
        return self.input.text().strip()

    def show_overlay(self) -> None:
        """定位到屏幕中上部并显示；返回时 `isVisible()` 已为 True。"""
        scr = self.screen() or self.parentWidget().screen() if self.parentWidget() else self.screen()
        try:
            geo = scr.availableGeometry()
        except Exception:
            geo = self.screen().availableGeometry()
        w = self._width()
        self._resize_to_content()
        x = geo.x() + (geo.width() - w) // 2
        y = geo.y() + int(geo.height() * OVERLAY_TOP_RATIO)
        self.move(QPoint(x, y))

        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus(Qt.OtherFocusReason)
        self._fade_in()
        log_event(LOG, "overlay_shown", x=x, y=y, w=w, h=self.height())

    def _fade_in(self) -> None:
        try:
            self._anim = QPropertyAnimation(self, b"windowOpacity", self)
            self._anim.setDuration(ANIM_MS)
            self._anim.setStartValue(0.0)
            self._anim.setEndValue(1.0)
            self._anim.setEasingCurve(QEasingCurve.OutCubic)
            self._anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception:  # noqa: BLE001
            self.setWindowOpacity(1.0)

    def hide_overlay(self) -> None:
        was = self.isVisible()
        self.hide()
        if was:
            log_event(LOG, "overlay_hidden")
            self.hidden.emit()

    def toggle(self) -> None:
        if self.isVisible():
            self.hide_overlay()
        else:
            self.show_overlay()

    def clear_input(self) -> None:
        self.input.clear()

    # ---------------------------------------------------------------- 事件
    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.hide_overlay()
            event.accept()
            return
        super().keyPressEvent(event)

    def event(self, event):
        et = event.type()
        if et == QEvent.WindowDeactivate and self._auto_hide and self.isVisible():
            # 失焦即隐（不打断当前工作）
            self.hide_overlay()
        return super().event(event)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.input and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.hide_overlay()
                return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event):  # noqa: N802
        event.ignore()
        self.hide_overlay()
