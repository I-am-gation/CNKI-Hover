"""原版阅读图像流视图（C3 产出）。

「原版阅读」在本项目里 = 机构授权 PDF 的**按页栅格化图像流**（数据源见 docs/api/read.md）。

实现要点：
- **按页懒加载**：只为「可视区 ±1 页」真正渲染 PNG，其余槽位只放占位块。
- **离屏释放**：滚出窗口的页立即 `clear_pixmap()`，内存不随翻阅页数增长。
- **缩放**：0.5×~3.0×；缩放只影响渲染 dpi 与槽位高度，不重排文档模型。
- **页码跳转**：`goto_page(n)`；带加载占位与帧日志。
- **帧日志**：`frame_log` 记录每页加载毫秒数，供 D1 与验收断言。

公共契约（C2 的 Tab 会挂载本控件）：
    pv = PageView(config)
    pv.open_item(item)                 # item 需有 detail_url / stable_id
    pv.goto_page(n) / pv.next_page() / pv.prev_page()
    pv.set_zoom(1.0) / pv.zoom_in() / pv.zoom_out()
    pv.page_count / pv.current_page / pv.frame_log
    pv.page_rendered: Signal[int, float]
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from cnki_api import reader as R
from . import theme
from .log import get_logger, log_event

LOG = get_logger("page_view")

BASE_WIDTH = 820           # 100% 时的逻辑页宽
PAGE_RATIO = 1.414         # A4 比例
DEFAULT_DPI = 150
# 内存上界：同时存活的页位图数量。
# 注意：加载哪些页由**视口**决定（滚到哪加载哪），不是固定的「当前页 ±N」，
# 所以这里给的是一个保守上界而非精确半径。
MAX_LIVE_PAGES = 8
# 单次渲染闸门：一次 _render_visible 最多真正渲染几页。
# 建完槽位时布局尚未跑，所有 slot.y() 都是 0，视口判定会误以为"全部可见"——
# 实测因此一次渲染 157 页、首页耗时 21 秒。此闸门 + 下面 _visible_range 的跨度兜底共同防住。
MAX_RENDER_PER_PASS = 6

QSS = """
#pvRoot { background: #101216; }
#pvBar { background: #16181D; border-bottom: 1px solid rgba(255,255,255,28); }
#pvBar QLabel { color: #9AA3B2; font-size: 12px; }
#pvBar QPushButton {
    background: #1F2229; border: 1px solid rgba(255,255,255,38); border-radius: 6px;
    color: #EDEFF3; padding: 4px 10px; font-size: 12px;
}
#pvBar QPushButton:hover { background: #262A33; }
#pvBar QPushButton:disabled { color: #5A6270; }
#pvSlot { background: #16181D; border: 1px solid rgba(255,255,255,22); }
#pvSlotText { color: #5A6270; font-size: 12px; }
QScrollArea { border: none; background: #101216; }
#pvBody { background: #101216; }
"""


class _PageSlot(QFrame):
    """单页槽位：未加载时显示占位，加载后显示图像。"""

    def __init__(self, page_no: int, owner: "PageView"):
        super().__init__(owner._body)
        self.setObjectName("pvSlot")
        self.page_no = page_no
        self.owner = owner
        self._pm: Optional[QPixmap] = None
        self.lb = QLabel("第 %d 页 · 加载中…" % page_no)
        self.lb.setObjectName("pvSlotText")
        self.lb.setAlignment(Qt.AlignCenter)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.addWidget(self.lb)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.set_logical_size(owner.logical_width(), owner.logical_height())

    def set_logical_size(self, w: int, h: int) -> None:
        """槽位尺寸**只由逻辑页尺寸决定**，绝不被位图尺寸反向撑大。

        早期实现用 `setFixedSize(pm.width(), pm.height())`，而位图是按固定 dpi 渲染的
        （实测 1240px 宽），逻辑页宽只有 820px → 槽位被撑到 1242px → 横向溢出 420px，
        用户看到的就是「原版阅读显示不完整一页」。
        """
        self._lw, self._lh = int(w), int(h)
        self.setFixedSize(self._lw, self._lh)
        self.lb.setFixedSize(self._lw, self._lh)

    def set_pixmap(self, pm: Optional[QPixmap]) -> None:
        self._pm = pm
        if pm is None:
            self.lb.setScaledContents(False)
            self.lb.setText("第 %d 页" % self.page_no)
            self.lb.setPixmap(QPixmap())
        else:
            self.lb.setText("")
            self.lb.setScaledContents(True)   # 位图等比缩放进槽位（配合 devicePixelRatio 保持清晰）
            self.lb.setPixmap(pm)
        self.set_logical_size(self._lw, self._lh)
        self.lb.update()

    def has_pixmap(self) -> bool:
        return self._pm is not None


class PageView(QWidget):
    page_rendered = Signal(int, float)     # (页码, 毫秒)

    def __init__(self, config=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config
        self.setObjectName("pvRoot")
        theme.bind(self, "pageview", QSS)

        self._item = None
        self._page_count = 0
        self._current = 1
        self._zoom = 1.0
        self._aspect = PAGE_RATIO          # 由真实 PDF 页面尺寸推导，避免比例不符导致留白/裁切
        self._auto_fit = True              # 自动整页适配窗口（缩放窗口时保持）
        self.frame_log: list = []
        self.render_calls = 0
        self.release_calls = 0
        self._slots: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("pvBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(6)

        self.btn_first = QPushButton("⏮")
        self.btn_prev = QPushButton("◀")
        self.btn_next = QPushButton("▶")
        self.btn_last = QPushButton("⏭")
        self.lb_page = QLabel("— / —")
        self.btn_zoom_out = QPushButton("－")
        self.lb_zoom = QLabel("100%")
        self.btn_zoom_in = QPushButton("＋")
        self.btn_fit = QPushButton("适应")
        for b in (self.btn_first, self.btn_prev, self.lb_page, self.btn_next, self.btn_last):
            bl.addWidget(b)
        bl.addStretch(1)
        for w in (self.btn_fit, self.btn_zoom_out, self.lb_zoom, self.btn_zoom_in):
            bl.addWidget(w)
        root.addWidget(bar)

        self.btn_first.clicked.connect(lambda: self.goto_page(1))
        self.btn_prev.clicked.connect(self.prev_page)
        self.btn_next.clicked.connect(self.next_page)
        self.btn_last.clicked.connect(lambda: self.goto_page(self._page_count))
        self.btn_fit.clicked.connect(self.fit_page)
        self.btn_zoom_out.clicked.connect(lambda: self.set_zoom(self._zoom / 1.2))
        self.btn_zoom_in.clicked.connect(lambda: self.set_zoom(self._zoom * 1.2))

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._body = QWidget()
        self._body.setObjectName("pvBody")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 8, 0, 8)
        self._body_layout.setSpacing(10)
        self._body_layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.scroll.setWidget(self._body)
        root.addWidget(self.scroll, 1)

        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self._update_controls()

    # ---------------------------------------------------------------- 尺寸
    def logical_width(self) -> int:
        return int(BASE_WIDTH * self._zoom)

    def logical_height(self) -> int:
        return int(BASE_WIDTH * self._aspect * self._zoom)

    # ---------------------------------------------------------------- 打开
    def open_item(self, item) -> None:
        self._item = item
        self._open_seq = getattr(self, "_open_seq", 0) + 1      # 防串门：渲染结果过期即弃
        self._page_count = 0
        self._current = 1
        self._clear_slots()
        self.lb_page.setText("— / —")
        sid = getattr(item, "stable_id", "") or ""
        try:
            self._page_count = int(R.get_page_count(
                getattr(item, "detail_url", ""), stable_id=sid))
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "open_failed", stable_id=sid, error=str(e)[:120])
            self._page_count = 0
        if self._page_count <= 0:
            self.lb_page.setText("无原版")
            self._update_controls()
            return
        # 用真实页面尺寸推导宽高比（不同期刊/学位论文开本不一，硬编码 1.414 会留白或裁切）
        try:
            w_pt, h_pt = R.page_size_pt(getattr(item, "detail_url", ""), page=1, stable_id=sid)
            if w_pt > 0 and h_pt > 0:
                self._aspect = float(h_pt) / float(w_pt)
        except Exception:  # noqa: BLE001
            pass
        self._build_slots()
        self.goto_page(1)
        self._auto_fit = True
        QTimer.singleShot(0, self.fit_page)     # 默认整页适配：一进来就能看全一整页
        log_event(LOG, "opened", stable_id=sid, pages=self._page_count,
                  aspect=round(self._aspect, 3))

    def _clear_slots(self) -> None:
        for s in self._slots:
            s.setParent(None)
            s.deleteLater()
        self._slots = []

    def _build_slots(self) -> None:
        self._clear_slots()
        for i in range(1, self._page_count + 1):
            slot = _PageSlot(i, self)
            self._body_layout.addWidget(slot, 0, Qt.AlignHCenter)
            self._slots.append(slot)
        # 立刻跑一次布局，让每个槽位的 y() 生效 —— 否则视口判定会以为"全部可见"
        self._body_layout.activate()

    # ---------------------------------------------------------------- 页操作
    @property
    def page_count(self) -> int:
        return self._page_count

    @property
    def current_page(self) -> int:
        return self._current

    def goto_page(self, n: int) -> None:
        if self._page_count <= 0:
            return
        n = max(1, min(self._page_count, int(n)))
        self._current = n
        self.lb_page.setText("%d / %d" % (n, self._page_count))
        self._ensure_visible(n)
        self._render_visible()
        self._update_controls()

    def next_page(self) -> None:
        self.goto_page(self._current + 1)

    def prev_page(self) -> None:
        self.goto_page(self._current - 1)

    def _ensure_visible(self, n: int) -> None:
        if 0 < n <= len(self._slots):
            self.scroll.ensureWidgetVisible(self._slots[n - 1], 0, 20)

    # ---------------------------------------------------------------- 懒加载（视口驱动）
    def _visible_range(self) -> tuple:
        """与视口相交（上下各外扩一屏）的页号区间 [lo, hi]（1 基）。

        ⚠️ 早期实现只渲染「当前页 ±1」，于是**滚轮往下翻到的页根本没被加载** ——
        用户一路滚下去看到的是大片"加载中"占位，表现为"只能显示部分页"。
        正解：以**视口**为准决定渲染谁，滚到哪就加载哪。
        """
        n = len(self._slots)
        if n == 0:
            return (0, 0)
        vp = self.scroll.viewport()
        top = self.scroll.verticalScrollBar().value()
        bottom = top + vp.height()
        margin = max(200, vp.height())
        lo = hi = None
        for idx, slot in enumerate(self._slots, start=1):
            y0 = slot.y()
            y1 = y0 + slot.height()
            if y1 >= top - margin and y0 <= bottom + margin:
                if lo is None:
                    lo = idx
                hi = idx
        if lo is None:
            return (self._current, self._current)
        # 兜底：区间跨度不得超过「视口能容纳的页数 ×3 + 2」。
        # 建完槽位、布局还没跑时所有 slot.y() 都是 0，上面的判定会认为全部可见。
        per = max(1, vp.height() // max(1, self.logical_height()))
        max_pages = max(4, per * 3 + 2)
        if hi - lo + 1 > max_pages:
            half = max_pages // 2
            lo2 = max(lo, min(self._current - half, hi - max_pages + 1))
            hi2 = min(hi, lo2 + max_pages - 1)
            lo, hi = lo2, hi2
        return (lo, hi)

    def _render_visible(self) -> None:
        """渲染可视区内的页；离开可视区的页释放位图（内存有界）。

        **优先渲染当前页**，并受 `MAX_RENDER_PER_PASS` 闸门限制，避免布局未就绪时一次性渲染整本。
        """
        lo, hi = self._visible_range()
        order = [self._current] + [i for i in range(lo, hi + 1) if i != self._current]
        rendered = 0
        for idx in order:
            if not (0 < idx <= len(self._slots)):
                continue
            slot = self._slots[idx - 1]
            if not slot.has_pixmap():
                self._render_one(idx)
                rendered += 1
                if rendered >= MAX_RENDER_PER_PASS:
                    break
        for idx, slot in enumerate(self._slots, start=1):
            if not (lo <= idx <= hi) and slot.has_pixmap():
                slot.set_pixmap(None)
                self.release_calls += 1
        self._sync_current_from_viewport()

    def _sync_current_from_viewport(self) -> None:
        """把"当前页"跟到视口中心所在的那一页（滚动时页码要跟着变）。"""
        if not self._slots:
            return
        vp = self.scroll.viewport()
        center = self.scroll.verticalScrollBar().value() + vp.height() // 2
        cur = self._current
        for idx, slot in enumerate(self._slots, start=1):
            if slot.y() <= center <= slot.y() + slot.height():
                cur = idx
                break
        if cur != self._current:
            self._current = cur
            self.lb_page.setText("%d / %d" % (cur, self._page_count))
            self._update_controls()

    def _render_one(self, n: int) -> None:
        """渲染第 n 页（1 基）并贴到对应槽位。

        带**两道防串门**（用户实测反馈"看 A 却显示 B"）：
        1. `self._open_seq` —— 渲染期间若又 open 了别的文献，结果直接丢弃；
        2. `img.stable_id` —— 渲染结果必须与当前文献一致才贴图。
        """
        item = self._item
        if item is None:
            return
        seq = self._open_seq
        cur_sid = getattr(item, "stable_id", "") or ""
        import time as _t
        t0 = _t.perf_counter()
        # 关键：**按目标像素宽度渲染**（逻辑宽 × 设备像素比），
        # 使位图宽度与槽位宽度精确一致 —— 既不会横向溢出被裁，也在高 DPI 屏上保持清晰。
        dpr = float(self.devicePixelRatioF() or 1.0)
        target_w = int(round(self.logical_width() * dpr))
        try:
            img = R.render_page(getattr(item, "detail_url", ""), page=n,
                                target_width=target_w,
                                stable_id=getattr(item, "stable_id", "") or "")
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "render_failed", page=n, error=str(e)[:120])
            if 0 < n <= len(self._slots):
                self._slots[n - 1].lb.setText("第 %d 页 · 加载失败" % n)
            return
        pm = QPixmap()
        if not pm.loadFromData(img.data):
            log_event(LOG, "pixmap_decode_failed", page=n)
            return
        pm.setDevicePixelRatio(dpr)      # 让 Qt 按逻辑尺寸显示，避免"看起来放大了一倍"
        # 防串校验：渲染期间又打开了别的文献（或结果不属于当前文献）→ 丢弃
        if seq != self._open_seq:
            log_event(LOG, "render_discarded_stale_open", page=n)
            return
        if cur_sid and getattr(img, "stable_id", cur_sid) != cur_sid:
            log_event(LOG, "render_discarded_wrong_item", page=n,
                      got=getattr(img, "stable_id", ""), want=cur_sid)
            return
        if 0 < n <= len(self._slots):
            self._slots[n - 1].set_pixmap(pm)
        cost_ms = (_t.perf_counter() - t0) * 1000
        self.render_calls += 1
        self.frame_log.append({"page": n, "ms": round(cost_ms, 1)})
        if len(self.frame_log) > 500:
            self.frame_log = self.frame_log[-500:]
        self.page_rendered.emit(n, cost_ms)

    def _on_scrolled(self, _v: int) -> None:
        """滚动 → 按**视口**决定加载/释放哪些页。

        这与早期实现的关键差别：以前只服务「当前页 ±1」，用户滚到下一页时那页还没被加载，
        于是一路看到"加载中"占位（表现为"只能显示部分页"）。现在滚到哪就加载哪。
        """
        if self._page_count <= 0 or not self._slots:
            return
        self._render_visible()

    # ---------------------------------------------------------------- 缩放 / 适配
    @property
    def zoom(self) -> float:
        return self._zoom

    def fit_page(self) -> None:
        """**整页适配窗口**：让一整页完整落在视口内（不必滚动就能看全）。

        用户诉求："应该直接把完整的拿下来" —— 默认 100% 时整页高于视口，只能看到一部分。
        """
        if self._page_count <= 0:
            return
        vp = self.scroll.viewport()
        avail_w = max(200, vp.width() - 26)
        avail_h = max(200, vp.height() - 26)
        z_w = avail_w / float(BASE_WIDTH)
        z_h = avail_h / float(BASE_WIDTH * self._aspect)
        self._auto_fit = True
        self.set_zoom(min(z_w, z_h, 1.5))

    def set_zoom(self, z: float) -> None:
        self._zoom = max(0.2, min(3.0, float(z)))
        self.lb_zoom.setText("%d%%" % int(round(self._zoom * 100)))
        w, h = self.logical_width(), self.logical_height()
        for s in self._slots:
            s.set_pixmap(None)
            s.set_logical_size(w, h)
        self.release_calls += len(self._slots)
        self._render_visible()
        self.lb_page.setText("%d / %d" % (self._current, self._page_count) if self._page_count else "— / —")

    def zoom_in(self) -> None:
        self._auto_fit = False
        self.set_zoom(self._zoom * 1.2)

    def zoom_out(self) -> None:
        self._auto_fit = False
        self.set_zoom(self._zoom / 1.2)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if self._auto_fit and self._page_count > 0:
            QTimer.singleShot(0, self.fit_page)
        else:
            QTimer.singleShot(0, self._render_visible)

    # ---------------------------------------------------------------- 状态
    def live_pixmaps(self) -> int:
        return sum(1 for s in self._slots if s.has_pixmap())

    def _update_controls(self) -> None:
        has = self._page_count > 0
        self.btn_first.setEnabled(has and self._current > 1)
        self.btn_prev.setEnabled(has and self._current > 1)
        self.btn_next.setEnabled(has and self._current < self._page_count)
        self.btn_last.setEnabled(has and self._current < self._page_count)

    def closeEvent(self, event):  # noqa: N802
        for s in self._slots:
            s.set_pixmap(None)
        super().closeEvent(event)
