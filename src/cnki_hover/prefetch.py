"""预取（C2 产出）。

策略：结果列表的**高亮项停留 ≥ delay_ms** 后，后台线程预热该文献
（详情页 → 原版 PDF → 正文文本，全部落本地缓存）。
这样用户真正按下 Enter 时，阅读窗口走的是**纯本地读盘**，做到「一点就出来」。

纪律：
- 预取**串行**、不并发轰炸；同一 stable_id 只预取一次（幂等）。
- 高亮快速移动时后一个请求会取消前一个（只在停留足够久时才真正发网络请求）。
- 预取走 `reader.read_html(..., stable_id=...)`，因此**同一文献永不重复下载 PDF**
  （稳定键见 reader.normalize_stable_id）。

公共契约：
    pf = Prefetcher(delay_ms=1000)
    pf.attach(navigator)                  # 监听 highlighter 变化
    pf.request(item) / pf.cancel()
    pf.prefetched: Signal[str, str]       # (stable_id, kind)
    pf.stats: dict                        # {"requests": n, "hits": n, "last_stable_id": s}
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from cnki_api import reader as R
from .log import get_logger, log_event

LOG = get_logger("prefetch")

DEFAULT_DELAY_MS = 1000


class _PrefetchWorker(QThread):
    done = Signal(str, str)   # stable_id, kind

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self._item = item

    def run(self) -> None:  # noqa: D102
        item = self._item
        sid = getattr(item, "stable_id", "") or ""
        try:
            if R.has_cached_text(sid):
                log_event(LOG, "prefetch_cache_hit", stable_id=sid)
                self.done.emit(sid, "cached")
                return
            c = R.read_html(item.detail_url, stable_id=sid)
            log_event(LOG, "prefetch_result", stable_id=sid, kind=c.kind, words=c.word_count)
            self.done.emit(sid, c.kind)
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "prefetch_failed", stable_id=sid, error=str(e)[:140])
            self.done.emit(sid, "error")


class Prefetcher(QObject):
    """高亮停留触发的后台预取器。"""

    prefetched = Signal(str, str)

    def __init__(self, delay_ms: int = DEFAULT_DELAY_MS, config=None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.config = config
        self.delay_ms = int(delay_ms)
        self.stats = {"requests": 0, "hits": 0, "errors": 0, "last_stable_id": "", "last_kind": ""}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self._pending = None
        self._worker: Optional[_PrefetchWorker] = None
        self._done_ids: set = set()
        self.enabled = True

    # ---------------------------------------------------------------- 绑定
    def attach(self, navigator=None, result_list=None) -> None:
        """挂到导航器（推荐）或直接挂结果列表。"""
        target = navigator if navigator is not None else result_list
        if target is None:
            return
        try:
            target.current_changed.connect(self._on_current_changed)
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "attach_failed", error=str(e)[:120])
        # 初始项也要有机会被预热
        try:
            if navigator is not None and navigator.current_item() is not None:
                self._on_current_changed(navigator.index, navigator.current_item())
        except Exception:  # noqa: BLE001
            pass

    def _on_current_changed(self, index: int, item) -> None:
        if not self.enabled or item is None:
            return
        sid = getattr(item, "stable_id", "") or ""
        if sid and sid in self._done_ids:
            return
        self._pending = item
        log_event(LOG, "prefetch_scheduled", index=index, stable_id=sid, delay_ms=self.delay_ms)
        self._timer.start(self.delay_ms)   # 停留 ≥delay_ms 才真正发起

    def cancel(self) -> None:
        self._timer.stop()
        self._pending = None
        log_event(LOG, "prefetch_cancelled")

    def request(self, item) -> None:
        """立即预取（跳过停留判定；供 D1/E2E 或显式调用）。"""
        self._pending = item
        self._timer.stop()
        self._fire()

    def _fire(self) -> None:
        item = self._pending
        self._pending = None
        if item is None or not self.enabled:
            return
        sid = getattr(item, "stable_id", "") or ""
        if not sid:
            return
        if self._worker is not None and self._worker.isRunning():
            # 串行：等上一次结束再补一次（避免并发轰炸知网）
            log_event(LOG, "prefetch_deferred", stable_id=sid)
            self._pending = item
            QTimer.singleShot(300, self._fire)
            return
        self.stats["requests"] += 1
        self.stats["last_stable_id"] = sid
        log_event(LOG, "prefetch_start", stable_id=sid, title=getattr(item, "title", "")[:40])
        self._worker = _PrefetchWorker(item, self)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, sid: str, kind: str) -> None:
        self._done_ids.add(sid)
        self.stats["last_kind"] = kind
        if kind in ("cached", R.KIND_HTML, R.KIND_ORIGINAL):
            self.stats["hits"] += 1
        elif kind == "error":
            self.stats["errors"] += 1
        log_event(LOG, "prefetch_done", stable_id=sid, kind=kind)
        self.prefetched.emit(sid, kind)
        # 若期间用户又移动了高亮，补做
        if self._pending is not None:
            QTimer.singleShot(50, self._fire)

    def wait_idle(self, timeout_ms: int = 20000) -> bool:
        if self._worker is not None and self._worker.isRunning():
            return self._worker.wait(timeout_ms)
        return True

    def clear(self) -> None:
        self._done_ids.clear()
