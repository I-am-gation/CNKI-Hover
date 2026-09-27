"""HTTP 阅读窗口 + 预取联动（C2 产出）。

独立窗口承载两种视图：
- **HTML 阅读**（默认）：`QTextBrowser` 原生富文本渲染正文文本，可选中、可检索。
- **原版阅读**：内嵌 C3 的 `PageView` 图像流（Tab 切换）。

「一点就出来」的三个手段（对应方案 §8）：
1. **预取**：结果列表高亮项停留即后台预热（见 prefetch.py）。
2. **文本优先 + 骨架屏**：窗口先出标题与骨架，正文到齐即替换；命中本地缓存时**同步渲染**，零网络。
3. **稳定缓存键**：同一文献永不重复下载（见 reader.normalize_stable_id）。

公共契约（C1 / D1 依赖）：
    w = ReaderWindow(config)
    w.open_item(item, tab="html")     # 立刻显示（骨架），随后填充
    w.set_tab("html" | "original")
    w.current_stable_id / w.current_title / w.first_paint_ms
    w.closed: Signal                  # Esc / 关闭 → 调用方把焦点还给原高亮位
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from cnki_api import reader as R
from . import theme
from .log import get_logger, log_event
from .page_view import PageView

LOG = get_logger("reader_window")

TAB_HTML = "html"
TAB_ORIGINAL = "original"

QSS = """
#rwRoot { background: #101216; }
#rwHead { background: #16181D; border-bottom: 1px solid rgba(255,255,255,28); }
#rwTitle { color: #EDEFF3; font-size: 15px; font-weight: 600; }
#rwMeta  { color: #9AA3B2; font-size: 12px; }
#rwTab { background: #1F2229; border: 1px solid rgba(255,255,255,38); border-radius: 7px;
         color: #9AA3B2; padding: 5px 14px; font-size: 12px; }
#rwTab[active="true"] { background: rgba(76,141,255,60); color: #EDEFF3; border-color: #4C8DFF; }
#rwStatus { color: #9AA3B2; font-size: 12px; padding: 6px 14px; background: #12141A; }
QTextBrowser { background: #101216; border: none; color: #DDE2EA; font-size: 15px; }
"""


class _LoadWorker(QThread):
    ok = Signal(object)
    bad = Signal(str)

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self._item = item

    def run(self) -> None:  # noqa: D102
        try:
            c = R.read_html(getattr(self._item, "detail_url", ""),
                            stable_id=getattr(self._item, "stable_id", "") or "")
            self.ok.emit(c)
        except Exception as e:  # noqa: BLE001
            self.bad.emit("%s: %s" % (type(e).__name__, e))


class ReaderWindow(QWidget):
    closed = Signal()

    def __init__(self, config=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config
        self.setObjectName("rwRoot")
        theme.bind(self, "reader", QSS)
        self.setWindowTitle("知网阅读")
        self.setWindowFlag(Qt.Window, True)
        self.resize(980, 760)

        self._item = None
        self._content = None
        self._worker: Optional[_LoadWorker] = None
        self._t_open = 0.0
        self.first_paint_ms: float = -1.0
        self.loads = 0
        self.cache_hits = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QFrame()
        head.setObjectName("rwHead")
        hl = QVBoxLayout(head)
        hl.setContentsMargins(14, 10, 12, 8)
        hl.setSpacing(6)

        self.lb_title = QLabel("—")
        self.lb_title.setObjectName("rwTitle")
        self.lb_title.setWordWrap(True)
        hl.addWidget(self.lb_title)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.lb_meta = QLabel("")
        self.lb_meta.setObjectName("rwMeta")
        row.addWidget(self.lb_meta, 1)
        self.btn_html = QPushButton("HTML 阅读")
        self.btn_orig = QPushButton("原版阅读")
        for b in (self.btn_html, self.btn_orig):
            b.setObjectName("rwTab")
            row.addWidget(b)
        hl.addLayout(row)
        root.addWidget(head)

        self.btn_html.clicked.connect(lambda: self.set_tab(TAB_HTML))
        self.btn_orig.clicked.connect(lambda: self.set_tab(TAB_ORIGINAL))

        self.stack = QStackedWidget()
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.page_view = PageView(config)
        self.stack.addWidget(self.browser)
        self.stack.addWidget(self.page_view)
        root.addWidget(self.stack, 1)

        self.lb_status = QLabel("")
        self.lb_status.setObjectName("rwStatus")
        root.addWidget(self.lb_status)

        self.set_tab(TAB_HTML, force=True)

    # ---------------------------------------------------------------- 状态
    @property
    def current_stable_id(self) -> str:
        return getattr(self._item, "stable_id", "") or "" if self._item else ""

    @property
    def current_title(self) -> str:
        return self.lb_title.text()

    @property
    def content(self):
        return self._content

    # ---------------------------------------------------------------- 打开
    def open_item(self, item, tab: str = TAB_HTML) -> None:
        """打开一条文献。**立即返回并已可见**（骨架屏），随后异步/同步填充正文。"""
        import time
        self._item = item
        self._t_open = time.perf_counter()
        self.first_paint_ms = -1.0
        self._content = None

        title = getattr(item, "title", "") or "（无标题）"
        self.lb_title.setText(title)
        meta = "%s · %s · %s · %s" % (
            getattr(item, "authors", "") or "—", getattr(item, "source", "") or "—",
            getattr(item, "year", "") or "—", getattr(item, "db_type", "") or "—")
        self.lb_meta.setText(meta)
        self.setWindowTitle("%s — 知网阅读" % title[:60])

        # 骨架屏
        self.browser.setHtml(
            "<html><body style=\"font-family:'Microsoft YaHei',sans-serif;color:#8A93A2;"
            "font-size:15px;line-height:2.0;margin:28px;\">"
            "<div style='font-size:17px;color:#DDE2EA;font-weight:600;'>%s</div>"
            "<p>%s</p><p>正在加载正文…</p></body></html>"
            % (title, meta))

        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.set_tab(tab, force=True)

        sid = self.current_stable_id
        # 命中本地缓存 → 同步渲染（零网络，首屏毫秒级）
        if R.has_cached_text(sid):
            self.cache_hits += 1
            self._render(R.read_html(getattr(item, "detail_url", ""), stable_id=sid))
            self.lb_status.setText("已从本地缓存呈现")
            log_event(LOG, "open_cached", stable_id=sid)
            return

        self.loads += 1
        self.lb_status.setText("正在取回正文…（首次需向知网请求，之后走本地缓存）")
        self._worker = _LoadWorker(item, self)
        self._worker.ok.connect(self._on_ok)
        self._worker.bad.connect(self._on_bad)
        self._worker.start()

    def _on_ok(self, content) -> None:
        self._render(content)
        self.lb_status.setText(content.message or "")

    def _on_bad(self, msg: str) -> None:
        self.browser.setHtml(
            "<html><body style=\"font-family:'Microsoft YaHei',sans-serif;color:#FF8A8A;"
            "margin:28px;\">取文失败：%s</body></html>" % msg)
        self.lb_status.setText("取文失败")

    def _render(self, content) -> None:
        import time
        self._content = content
        if content.kind == R.KIND_HTML and content.word_count > 0:
            self.browser.setHtml(content.html)
            self.lb_status.setText("%s（%d 字 / 共 %d 页）"
                                   % (content.message, content.word_count, content.pages))
        else:
            tip = content.message or "该文献暂无可读正文"
            self.browser.setHtml(
                "<html><body style=\"font-family:'Microsoft YaHei',sans-serif;color:#C9CFD9;"
                "font-size:15px;line-height:2.0;margin:28px;\">"
                "<p style='color:#FFCC66;'>%s</p>"
                "<p style='color:#8A93A2;'>可尝试切到「原版阅读」按页查看（若该文献有原版）。</p>"
                "</body></html>" % tip)
            self.lb_status.setText(tip)
        # 强制完成一次排版+绘制，使 first_paint_ms 真正对应「屏幕上已看到」
        # （setHtml 只是设置文档，Qt 的富文本排版是惰性的，会在首次绘制时才发生）
        try:
            self.browser.repaint()
            self.repaint()
        except Exception:  # noqa: BLE001
            pass
        if self.first_paint_ms < 0:
            self.first_paint_ms = (time.perf_counter() - self._t_open) * 1000

    # ---------------------------------------------------------------- Tab
    def set_tab(self, tab: str, force: bool = False) -> None:
        tab = TAB_ORIGINAL if tab == TAB_ORIGINAL else TAB_HTML
        if not force and getattr(self, "_tab", None) == tab:
            return
        self._tab = tab
        if tab == TAB_HTML:
            self.stack.setCurrentWidget(self.browser)
        else:
            self.stack.setCurrentWidget(self.page_view)
            # ⚠️ 判据曾写成 `page_count <= 0` —— 那是「从未打开过」的意思。
            # 后果：打开 A 之后 page_count>0，再打开 B 时**根本不会重新 open_item**，
            # 原版 Tab 永远停在 A 的页（用户反馈"没法刷新更新"）。
            # 正解：**当前显示的不是这条文献**就重新打开。
            if self._item is not None and self.page_view.item is not self._item:
                self.page_view.open_item(self._item)
        for b, name in ((self.btn_html, TAB_HTML), (self.btn_orig, TAB_ORIGINAL)):
            b.setProperty("active", "true" if name == tab else "false")
            b.style().unpolish(b)
            b.style().polish(b)

    @property
    def current_tab(self) -> str:
        return getattr(self, "_tab", TAB_HTML)

    # ---------------------------------------------------------------- 事件
    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.hide()
            self.closed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):  # noqa: N802
        event.ignore()
        self.hide()
        self.closed.emit()
