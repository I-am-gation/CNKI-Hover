"""结果列表 + 检索流程集成（B3 产出）。

把「悬浮窗输入」接到「知网检索」再接回「结果列表」：

    输入框 textChanged ──300ms 防抖──► SearchController
                                          │  ①解析前缀（作者:/篇名:…）
                                          │  ②cnki_api.search.search()（QThread 里跑，不卡 UI）
                                          ▼
                                      ResultListWidget
                                          ├─ loading / empty / error / ready 四态
                                          └─ 两行制结果行（标题 + 元信息），整行高亮

公共契约（下游 C1 / C2 / C3 / D1 依赖）：
    rl = ResultListWidget(config)
    rl.set_items(items) / rl.set_state(state, message="")
    rl.current_index / current_item() / set_current_index(i)
    rl.rows: list[ResultRow]
    rl.item_activated: Signal[int, object]      # 点击/回车 → (index, SearchItem)

    sc = SearchController(overlay, config)
    sc.run_search(text, use_cache=True) -> SearchResult     # 同步（供验收/预取）
    sc.on_text_changed(text)                                # 防抖异步
    sc.results_ready: Signal(object)   sc.failed: Signal(str)
    sc.last_cost_ms: float
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from cnki_api import search as S
from . import theme
from .config import Config, load_config
from .log import get_logger, log_event

LOG = get_logger("result_list")

STATE_IDLE = "idle"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_EMPTY = "empty"
STATE_ERROR = "error"

ROW_HEIGHT = 58
LIST_MAX_HEIGHT = 430
DEBOUNCE_MS = 300

QSS = """
#rlRoot { background: transparent; }
QScrollArea { background: transparent; border: none; }
#rlScrollBody { background: transparent; }
#rlHint { color: #9AA3B2; font-size: 13px; padding: 16px 18px; }
#rlErr  { color: #FF8A8A; font-size: 13px; padding: 16px 18px; }
#rlRow { background: transparent; border-radius: 9px; }
#rlRow:hover { background: rgba(255,255,255,16); }
#rlRow[selected="true"] { background: rgba(76,141,255,52); }
#rlTitle { color: #EDEFF3; font-size: 14px; }
#rlMeta  { color: #9AA3B2; font-size: 12px; }
#rlBadge { color: #4C8DFF; font-size: 11px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,46); border-radius: 4px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""


def _elide(text: str, width: int, fm: QFontMetrics) -> str:
    return fm.elidedText(text or "", Qt.ElideRight, max(40, width))


@dataclass
class _RowData:
    index: int
    item: object


class ResultRow(QFrame):
    """两行制结果行：标题 + 元信息。整行可高亮（与键盘高亮同源）。"""

    hovered = Signal(int)
    activated = Signal(int)

    def __init__(self, index: int, item, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("rlRow")
        self.setProperty("selected", "false")
        self.index = index
        self.item = item
        self.setFixedHeight(ROW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 7, 12, 7)
        lay.setSpacing(2)

        self.lb_title = QLabel()
        self.lb_title.setObjectName("rlTitle")
        lay.addWidget(self.lb_title)

        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(8)
        self.lb_meta = QLabel()
        self.lb_meta.setObjectName("rlMeta")
        meta.addWidget(self.lb_meta, 1)
        self.lb_badge = QLabel()
        self.lb_badge.setObjectName("rlBadge")
        meta.addWidget(self.lb_badge, 0)
        lay.addLayout(meta)

        self.set_item(index, item)

    def set_item(self, index: int, item) -> None:
        self.index = index
        self.item = item
        authors = getattr(item, "authors", "") or "—"
        source = getattr(item, "source", "") or "—"
        year = getattr(item, "year", "") or "—"
        cited = getattr(item, "cited", 0)
        dl = getattr(item, "downloads", 0)
        self.lb_title.setText(_elide(getattr(item, "title", ""), 640, QFontMetrics(self.lb_title.font())))
        self.lb_meta.setText("%s · %s · %s · 被引 %s · 下载 %s" % (authors, source, year, cited, dl))
        self.lb_badge.setText(getattr(item, "db_type", "") or "")

    def set_selected(self, on: bool) -> None:
        self.setProperty("selected", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    # ---- 鼠标通道：悬停与点击（C1 会把它们接到同一状态机） ----
    def enterEvent(self, event):  # noqa: N802
        self.hovered.emit(self.index)
        super().enterEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.activated.emit(self.index)
            event.accept()
            return
        super().mousePressEvent(event)


class ResultListWidget(QWidget):
    """结果列表（含 loading / empty / error / ready 四态）。"""

    item_activated = Signal(int, object)
    current_changed = Signal(int, object)
    hover_changed = Signal(int, object)
    layout_changed = Signal()          # 高度/内容变化 → 让宿主（悬浮窗）重新量高

    def __init__(self, config: Optional[Config] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config or load_config()
        self.setObjectName("rlRoot")
        theme.bind(self, "result", QSS)

        self._items: list = []
        self.rows: list[ResultRow] = []
        self._current = -1
        self._state = STATE_IDLE

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 6)
        root.setSpacing(0)

        self.hint = QLabel("")
        self.hint.setObjectName("rlHint")
        self.hint.setVisible(False)
        root.addWidget(self.hint)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setMaximumHeight(LIST_MAX_HEIGHT)
        self.scroll.setVisible(False)

        self._body = QWidget()
        self._body.setObjectName("rlScrollBody")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(2)
        self._body_layout.addStretch(1)
        self.scroll.setWidget(self._body)
        root.addWidget(self.scroll)

        self.set_state(STATE_IDLE)

    # ---------------------------------------------------------------- 状态
    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str, message: str = "") -> None:
        self._state = state
        if state == STATE_LOADING:
            self.hint.setObjectName("rlHint")
            self.hint.setText(message or "检索中…")
            self.hint.setVisible(True)
            self.scroll.setVisible(False)
        elif state == STATE_EMPTY:
            self.hint.setObjectName("rlHint")
            self.hint.setText(message or "未找到相关文献，换个关键词试试")
            self.hint.setVisible(True)
            self.scroll.setVisible(False)
        elif state == STATE_ERROR:
            self.hint.setObjectName("rlErr")
            self.hint.setText(message or "检索失败")
            self.hint.setVisible(True)
            self.scroll.setVisible(False)
        elif state == STATE_READY:
            self.hint.setVisible(False)
            self.scroll.setVisible(True)
        else:
            self.hint.setVisible(False)
            self.scroll.setVisible(False)
        self.hint.style().unpolish(self.hint)
        self.hint.style().polish(self.hint)
        self._apply_height()

    # ---------------------------------------------------------------- 高度自适应
    def desired_list_height(self) -> int:
        """按行数算出列表应有高度（上限 LIST_MAX_HEIGHT）。"""
        n = len(self.rows)
        if n <= 0 or self._state != STATE_READY:
            return 0
        return min(LIST_MAX_HEIGHT, n * (ROW_HEIGHT + 2) + 8)

    def _apply_height(self) -> None:
        """把列表高度锁到内容所需值，并通知宿主重新量高。

        没有这一步时：发起检索那一刻列表还是空的 → 悬浮窗按空列表算高度 →
        结果到达后**没人再 resize**，用户只看到第一行（其余被窗口裁掉），
        只有收起再唤出（`show_overlay()` 会重新量高）才恢复正常。
        """
        h = self.desired_list_height()
        if h > 0:
            self.scroll.setFixedHeight(h)
        else:
            self.scroll.setMinimumHeight(0)
            self.scroll.setMaximumHeight(LIST_MAX_HEIGHT)
        lay = self.layout()
        if lay is not None:
            lay.activate()
        self.updateGeometry()
        self.layout_changed.emit()

    # ---------------------------------------------------------------- 数据
    def set_items(self, items: list) -> None:
        self._clear_rows()
        self._items = list(items or [])
        for i, it in enumerate(self._items):
            row = ResultRow(i, it, self._body)
            row.hovered.connect(self._on_row_hover)
            row.activated.connect(self._on_row_click)
            self._body_layout.insertWidget(i, row)
            self.rows.append(row)
        if self._items:
            self.set_state(STATE_READY)
            self.set_current_index(0)
        else:
            self.set_state(STATE_EMPTY)
        log_event(LOG, "results_set", n=len(self._items))

    def _clear_rows(self) -> None:
        for row in self.rows:
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        self._items = []
        self._current = -1

    @property
    def items(self) -> list:
        return list(self._items)

    # ---------------------------------------------------------------- 当前项
    @property
    def current_index(self) -> int:
        return self._current

    def current_item(self):
        if 0 <= self._current < len(self._items):
            return self._items[self._current]
        return None

    def set_current_index(self, idx: int, notify: bool = True) -> None:
        if not self._items:
            self._current = -1
            return
        idx = max(0, min(len(self._items) - 1, int(idx)))
        if idx == self._current:
            return
        self._current = idx
        for i, row in enumerate(self.rows):
            row.set_selected(i == idx)
        # 保证当前项可见
        try:
            self.scroll.ensureWidgetVisible(self.rows[idx], 0, 8)
        except Exception:  # noqa: BLE001
            pass
        if notify:
            self.current_changed.emit(idx, self._items[idx])

    def select_next(self) -> None:
        if self._items:
            self.set_current_index(min(self._current + 1, len(self._items) - 1))

    def select_prev(self) -> None:
        if self._items:
            self.set_current_index(max(self._current - 1, 0))

    # ---------------------------------------------------------------- 事件
    def _on_row_hover(self, idx: int) -> None:
        # 鼠标悬停与键盘高亮**同源**：都改同一个「当前项」
        self.set_current_index(idx)
        if 0 <= idx < len(self._items):
            self.hover_changed.emit(idx, self._items[idx])

    def _on_row_click(self, idx: int) -> None:
        self.set_current_index(idx, notify=False)
        if 0 <= idx < len(self._items):
            self.item_activated.emit(idx, self._items[idx])

    def keyPressEvent(self, event):  # noqa: N802
        k = event.key()
        if k in (Qt.Key_Down, Qt.Key_Up):
            self.select_next() if k == Qt.Key_Down else self.select_prev()
            event.accept()
            return
        if k in (Qt.Key_Return, Qt.Key_Enter):
            if self.current_item() is not None:
                self.item_activated.emit(self._current, self.current_item())
            event.accept()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------- 检索流程

class _SearchWorker(QThread):
    ok = Signal(object)
    bad = Signal(str)

    def __init__(self, controller: "SearchController", text: str, use_cache: bool):
        super().__init__(controller)
        self._ctl = controller
        self._text = text
        self._use_cache = use_cache

    def run(self) -> None:  # noqa: D102
        try:
            res = self._ctl.run_search(self._text, use_cache=self._use_cache)
            self.ok.emit(res)
        except Exception as e:  # noqa: BLE001
            self.bad.emit("%s: %s" % (type(e).__name__, e))


class SearchController(QObject):
    """输入 → 防抖 → 检索 → 列表。"""

    results_ready = Signal(object)
    failed = Signal(str)
    started = Signal(str)

    def __init__(self, overlay=None, config: Optional[Config] = None, client=None,
                 result_list: Optional[ResultListWidget] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.config = config or load_config()
        self.overlay = overlay
        self.result_list = result_list
        self.client = client
        self._owned_client = None          # 自建的持久客户端（避免每次检索都重做认证探针）
        self.last_cost_ms: float = 0.0
        self.last_http_ms: float = 0.0      # 纯网络耗时（不含节流等待），用于归因
        self.last_throttle_ms: float = 0.0  # 本次为满足低频纪律而等待的毫秒数
        self.search_seq: int = 0            # 检索序号：每次 run_search 自增，供上层丢弃陈旧结果
        self.last_query: str = ""
        self.last_field: str = "SU"
        self.last_error: str = ""
        self._cache: dict = {}
        self._worker: Optional[_SearchWorker] = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fire)

        if overlay is not None:
            try:
                overlay.input.textChanged.connect(self.on_text_changed)
                overlay.submitted.connect(lambda _t: self._fire())
                # 切检索项 → 立即用新字段重检（用户直接从下拉框选「作者」等）
                overlay.field_changed.connect(lambda _c: self._fire())
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 同步检索
    def _client(self):
        if self.client is not None:
            return self.client
        # 关键：持久复用同一个已认证客户端。
        # 否则每次检索都会重新 from_store() + 在线探针（实测 +2s），首屏必然超 1.5s。
        if self._owned_client is None:
            from cnki_api.auth import get_authenticated_client  # noqa: PLC0415
            self._owned_client = get_authenticated_client()
        return self._owned_client

    def warmup(self) -> bool:
        """预热：提前建立认证会话（应用启动/登录后调用），使首次检索不等认证探针。"""
        try:
            self._client()
            return True
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "warmup_failed", error=str(e)[:120])
            return False

    @staticmethod
    def parse(text: str):
        """前缀语法："作者:张三" → (field, value, label)。"""
        return S.parse_prefix(text)

    def _resolve(self, text: str):
        """解析出本次检索的 (field, value, label)。

        - 输入里带**有效**前缀（`作者:xx` / `作者.xx`）→ 用前缀指定的字段，并回显到下拉框；
        - 否则 → 用**下拉框**当前选中的字段（用户要求：像知网一样选，不手打字段名）。
        """
        t = (text or "").strip()
        if S.has_prefix(t):
            code, value, label = S.parse_prefix(t)
            if self.overlay is not None:
                try:
                    self.overlay.set_field(code)      # 内部 blockSignals，不会触发重检
                except Exception:  # noqa: BLE001
                    pass
            return code, value, label
        field = "SU"
        if self.overlay is not None:
            try:
                field = self.overlay.current_field()
            except Exception:  # noqa: BLE001
                field = "SU"
        label = next((lb for lb, cd in getattr(self.overlay, "FIELD_CHOICES", []) if cd == field),
                     field) if self.overlay is not None else field
        return field, t, label

    def run_search(self, text: str, use_cache: bool = True):
        """同步执行一次检索（供验收 / 预取 / 断网降级测试）。失败抛 SearchError。"""
        field, value, label = self._resolve(text)
        if not value:
            raise S.SearchError("请输入检索词")
        key = (field, value)
        self.last_field = field
        self.last_query = value
        self.search_seq += 1
        if use_cache and key in self._cache:
            self.last_cost_ms = 0.4
            log_event(LOG, "search_cache_hit", field=field, value=value[:30])
            return self._cache[key]
        t0 = time.monotonic()
        res = S.search(keyword=value, field=field, client=self._client(),
                       page_size=int(self.config.get("page_size", 20) or 20))
        self.last_cost_ms = (time.monotonic() - t0) * 1000
        self.last_http_ms = float(getattr(self._client(), "last_cost_ms", 0.0))
        self.last_throttle_ms = float(getattr(self._client(), "last_waited_ms", 0.0))
        if use_cache:
            self._cache[key] = res
        log_event(LOG, "search_done", field=field, total=getattr(res, "total", 0),
                  items=len(getattr(res, "items", [])), cost_ms=round(self.last_cost_ms, 1))
        return res

    # ---------------------------------------------------------------- 防抖异步
    def _cache_key_for(self, text: str):
        field, value, _label = self._resolve(text)
        return (field, value) if value else None

    def on_text_changed(self, text: str) -> None:
        """300ms 防抖；但**已缓存过的查询立即出结果**（不等防抖，保证命中缓存 ≤300ms）。"""
        # 立即给出「骨架屏」反馈：用户敲第一个字就有响应，不必等防抖+网络
        if self.result_list is not None and (text or "").strip():
            if self.result_list.state != STATE_LOADING:
                self.result_list.set_state(STATE_LOADING)
            if self.overlay is not None:
                try:
                    self.overlay.set_body_visible(True)
                except Exception:  # noqa: BLE001
                    pass
        key = self._cache_key_for(text)
        if key is not None and key in self._cache:
            self._debounce.stop()
            QTimer.singleShot(0, self._fire)
            return
        self._debounce.start()   # 连续输入只发最后一次

    def _fire(self) -> None:
        if self.overlay is None:
            return
        text = self.overlay.current_query()
        if not text:
            if self.result_list is not None:
                self.result_list.set_state(STATE_IDLE)
                self.result_list.set_items([])
            return
        self.started.emit(text)
        if self.result_list is not None:
            self.result_list.set_state(STATE_LOADING)
        if self.overlay is not None:
            self.overlay.set_body_visible(True)
        self._worker = _SearchWorker(self, text, use_cache=True)
        self._worker.ok.connect(self._on_ok)
        self._worker.bad.connect(self._on_bad)
        self._worker.start()

    def _on_ok(self, res) -> None:
        self.last_error = ""
        if self.result_list is not None:
            self.result_list.set_items(res.items)
        self.results_ready.emit(res)

    def _on_bad(self, msg: str) -> None:
        self.last_error = msg
        log_event(LOG, "search_failed", error=msg[:140])
        if self.result_list is not None:
            self.result_list.set_state(STATE_ERROR, "检索失败：%s\n（检查网络或登录状态后重试）" % msg)
        self.failed.emit(msg)

    def retry(self) -> None:
        self._fire()

    def clear_cache(self) -> None:
        self._cache.clear()

    def wait_idle(self, timeout_ms: int = 8000) -> bool:
        """供验收/预取等待异步检索结束。"""
        if self._worker is not None and self._worker.isRunning():
            return self._worker.wait(timeout_ms)
        return True
