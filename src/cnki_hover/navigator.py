"""键鼠双通道导航（C1 产出）。

核心命题：**键盘与鼠标驱动的是同一个「当前项」状态机**，不是两套并行逻辑。
- 键盘：`↑↓` → `move(-1/+1)`
- 鼠标：悬停 → `hover(i)`
- 确认：`Enter` / 左键 → `confirm()`

**滚轮语义（按用户实测反馈确定）**：默认 **滚轮 = 滚动列表视图**（像聊天记录一样连续下滑），
**不移动选中项** —— 所以默认不拦截滚轮，交给 `QScrollArea` 原生滚动。
若需要旧行为（滚轮移动高亮），把 config `wheel_moves_selection` 设为 true 即可。

边界行为：**夹止（clamp）**，首项再上/末项再下均停住，行为确定可断言。

公共契约（C2 / D1 依赖）：
    nav = Navigator(config)
    nav.attach(result_list)          # 绑定到 ResultListWidget
    nav.move(delta) / move_wheel(n) / hover(i) / confirm()
    nav.index / nav.current_item() / nav.set_index(i)
    nav.current_changed: Signal[int, object]
    nav.activated:       Signal[int, object]
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QWidget

from .log import get_logger, log_event

LOG = get_logger("navigator")

WHEEL_NOTCH = 120   # 标准滚轮一格 = 120/8 度


class Navigator(QObject):
    """键鼠共享的「当前项」状态机。"""

    current_changed = Signal(int, object)
    activated = Signal(int, object)

    def __init__(self, config=None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.config = config
        self._list = None
        self._index = -1
        self._wheel_accum = 0
        self.last_source = ""      # "keyboard" | "wheel" | "hover" | "init"
        # 默认 false：滚轮=滚动列表视图；true 才用滚轮移动选中项（旧行为）
        self.wheel_moves_selection = False
        if config is not None:
            try:
                self.wheel_moves_selection = bool(config.get("wheel_moves_selection", False))
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 绑定
    def attach(self, result_list) -> None:
        """绑定结果列表：悬停/点击回流到本状态机，本状态机回推高亮。"""
        self._list = result_list
        try:
            result_list.hover_changed.connect(self._on_list_hover)
            result_list.item_activated.connect(self._on_list_activated)
            result_list.current_changed.connect(self._on_list_current)
            self.install_wheel_filter(result_list)
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "attach_failed", error=str(e)[:120])
        self.refresh()

    def install_wheel_filter(self, widget: QWidget) -> None:
        """是否接管滚轮事件。

        **默认不接管** —— 滚轮交给 `QScrollArea` 原生滚动（用户要的"像聊天记录一样滑动"）。
        只有显式开启 `wheel_moves_selection` 时才把滚轮改道为"移动选择"（旧行为）。
        """
        if not self.wheel_moves_selection:
            return
        targets = [widget]
        for attr in ("scroll", "_body"):
            w = getattr(widget, attr, None)
            if w is not None:
                targets.append(w)
                vp = getattr(w, "viewport", None)
                if callable(vp):
                    targets.append(vp())
        for t in targets:
            if t is not None:
                t.installEventFilter(self)

    # ---------------------------------------------------------------- 状态
    def refresh(self) -> None:
        n = self.count()
        if n == 0:
            self._set_index(-1, "init", notify=True)
        elif self._index < 0:
            self._set_index(0, "init", notify=True)

    def count(self) -> int:
        if self._list is None:
            return 0
        try:
            return len(self._list.items)
        except Exception:  # noqa: BLE001
            return 0

    @property
    def index(self) -> int:
        return self._index

    def current_item(self):
        if self._list is None or not (0 <= self._index < self.count()):
            return None
        try:
            return self._list.items[self._index]
        except Exception:  # noqa: BLE001
            return None

    def items(self) -> list:
        if self._list is None:
            return []
        try:
            return list(self._list.items)
        except Exception:  # noqa: BLE001
            return []

    # ---------------------------------------------------------------- 变更入口
    def _set_index(self, idx: int, source: str, notify: bool = True) -> int:
        n = self.count()
        if n == 0:
            self._index = -1
            return -1
        idx = max(0, min(n - 1, int(idx)))   # 夹止
        if idx == self._index and source == self.last_source:
            return self._index
        self._index = idx
        self.last_source = source
        if self._list is not None:
            try:
                self._list.set_current_index(idx, notify=False)
            except Exception:  # noqa: BLE001
                pass
        if notify:
            self.current_changed.emit(idx, self.current_item())
        return self._index

    def set_index(self, idx: int, source: str = "init") -> int:
        return self._set_index(idx, source)

    # ---- 键盘 ----
    def move(self, delta: int) -> int:
        """键盘 ↑↓：delta=-1 上一项，+1 下一项。**夹止**。"""
        return self._set_index(self._index + int(delta), "keyboard")

    def next(self) -> int:
        return self.move(1)

    def prev(self) -> int:
        return self.move(-1)

    # ---- 滚轮 ----
    def move_wheel(self, notches: int) -> int:
        """滚轮：正数=向下=后一项；负数=向上=前一项。与键盘共用同一状态机。"""
        return self._set_index(self._index + int(notches), "wheel")

    # ---- 悬停 ----
    def hover(self, idx: int) -> int:
        """鼠标悬停：与键盘高亮**同源**（改同一个当前项）。"""
        return self._set_index(idx, "hover")

    # ---- 确认 ----
    def confirm(self) -> bool:
        item = self.current_item()
        if item is None:
            return False
        log_event(LOG, "confirm", index=self._index, source=self.last_source)
        self.activated.emit(self._index, item)
        return True

    # ---------------------------------------------------------------- 回流
    def _on_list_hover(self, idx: int, item) -> None:
        self._set_index(idx, "hover")

    def _on_list_activated(self, idx: int, item) -> None:
        self._set_index(idx, "hover")
        self.activated.emit(idx, item)

    def _on_list_current(self, idx: int, item) -> None:
        # 列表自身（如 rebuild）改了当前项 → 同步，避免状态分叉
        if idx != self._index:
            self._index = idx

    # ---------------------------------------------------------------- 事件过滤
    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.Wheel:
            d = event.angleDelta().y()
            if d == 0:
                d = event.pixelDelta().y()
            if d != 0:
                self._wheel_accum += d
                steps = int(self._wheel_accum / WHEEL_NOTCH)
                if steps != 0:
                    self._wheel_accum -= steps * WHEEL_NOTCH
                    # 注意符号：Windows 下向下滚 angleDelta().y() 为**负**，
                    # 而语义上「向下滚 = 移到下一项」→ 取反后再交给 move_wheel。
                    self.move_wheel(-steps)
                event.accept()
                return True
        return super().eventFilter(obj, event)
