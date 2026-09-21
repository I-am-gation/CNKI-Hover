"""C1 键鼠双通道导航验收（可判定自动化事件测试）。

跑法：
    .venv/Scripts/python.exe tests/test_navigator.py
也兼容 pytest：  .venv/Scripts/python.exe -m pytest tests/test_navigator.py -q

判定项：
 1. path_equivalence —— 同一初始态下，**纯键盘序列**与**纯鼠标序列**（滚轮+悬停）
                        驱动同一状态机，断言两条路径**终态一致**（索引 + 当前项一致）
 2. confirm_same     —— 两条路径 confirm() 打开的是**同一条文献**
 3. mixed_path       —— 键盘/滚轮/悬停混用后状态仍自洽（无缝切换通道）
 4. boundaries       —— 首/末项夹止行为确定
 5. wheel_event      —— 真实 QWheelEvent 打到列表视口上 → 路由为「移动选择」而非滚动视图
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cnki_api.search import SearchItem  # noqa: E402
from cnki_hover.navigator import Navigator  # noqa: E402
from cnki_hover.result_list import ResultListWidget  # noqa: E402

N = 20


def _mk_items(n: int = N):
    return [
        SearchItem(title="文献 %02d" % i, authors="作者%d" % i, source="来源%d" % i,
                   year="202%d" % (i % 10), cited=i, downloads=i * 2,
                   db_type="期刊", detail_url="https://example.invalid/%d" % i)
        for i in range(n)
    ]


def _new_nav(wheel_moves: bool = False):
    rl = ResultListWidget()
    rl.resize(760, 470)
    rl.show()                       # 必须真正显示，QScrollArea 才会算出滚动范围
    rl.set_items(_mk_items())
    QApplication.processEvents()
    nav = Navigator(_Cfg(wheel_moves_selection=wheel_moves))
    nav.attach(rl)
    QApplication.processEvents()
    return nav, rl


class _Cfg:
    """最小 config 替身（Navigator 只用到 get）。"""

    def __init__(self, **kw):
        self._d = kw

    def get(self, k, default=None):
        return self._d.get(k, default)


# --------------------------------------------------------------------- 判定项

def test_path_equivalence():
    """纯键盘序列 vs 纯鼠标（悬停）序列 → 终态一致。"""
    kb, _ = _new_nav()
    ms, _ = _new_nav()

    kb_seq = [1, 1, 1, -1, 1, 1]
    for d in kb_seq:
        kb.move(d)
    kb_end, kb_item = kb.index, kb.current_item()

    # 鼠标通道：悬停到第 4 条（0 基）—— 与键盘序列等价的目标位
    ms.hover(4)
    ms_end, ms_item = ms.index, ms.current_item()

    assert kb_end == ms_end == 4, "键盘终态=%s 鼠标悬停终态=%s（期望 4）" % (kb_end, ms_end)
    assert kb_item.title == ms_item.title, "两条路径当前项不一致：%r vs %r" % (kb_item.title, ms_item.title)
    return ("键盘序列 %s 与鼠标悬停 均到 index=%d；当前项=%r；两通道共用同一当前项状态"
            % (kb_seq, kb_end, kb_item.title))


def test_confirm_same():
    kb, _ = _new_nav()
    ms, _ = _new_nav()
    for _ in range(3):
        kb.move(+1)
    ms.hover(3)

    got = {}
    kb.activated.connect(lambda i, it: got.setdefault("kb", (i, it.title)))
    ms.activated.connect(lambda i, it: got.setdefault("ms", (i, it.title)))
    kb.confirm()
    ms.confirm()
    assert got["kb"] == got["ms"], "确认打开的不是同一条：%r vs %r" % (got["kb"], got["ms"])
    return "键盘与鼠标 confirm() 打开同一文献 %r" % (got["kb"],)


def test_mixed_path():
    """键盘 + 滚轮 API + 悬停混用，状态自洽。"""
    nav, _ = _new_nav()
    nav.move(+1)          # 1
    nav.move_wheel(+2)    # 3   （API 仍在，供显式调用/旧行为）
    nav.hover(10)         # 10
    nav.move(-1)          # 9
    nav.move_wheel(-4)    # 5
    assert nav.index == 5, "混用后序号=%s（期望 5）" % nav.index
    assert nav.last_source == "wheel", "最后一次来源=%r" % nav.last_source
    return "键盘→滚轮API→悬停→键盘→滚轮API 后 index=5，来源=%s" % nav.last_source


def test_boundaries():
    nav, _ = _new_nav()
    assert nav.index == 0, "初始序号应为 0，实际 %s" % nav.index
    nav.move(-1)
    a = nav.index
    nav.move(-5)
    b = nav.index
    for _ in range(N + 5):
        nav.move(+1)
    c = nav.index
    nav.move(+9)
    d = nav.index
    assert a == b == 0, "首项夹止失效：%s / %s" % (a, b)
    assert c == d == N - 1, "末项夹止失效：%s / %s（期望 %s）" % (c, d, N - 1)
    return "首项夹止=%s/%s，末项夹止=%s/%s（N=%d）" % (a, b, c, d, N)


def test_wheel_scrolls_not_selects():
    """默认：真实滚轮事件 → **滚动列表视图**，不移动选中项（用户实测反馈的诉求）。"""
    nav, rl = _new_nav()
    viewport = rl.scroll.viewport()
    sb = rl.scroll.verticalScrollBar()
    before_scroll = sb.value()
    idx_before = nav.index
    for _ in range(3):
        ev = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, -120),
                         Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
        QApplication.sendEvent(viewport, ev)
    after_scroll = sb.value()
    assert after_scroll > before_scroll, "滚轮后视图未滚动：%s -> %s" % (before_scroll, after_scroll)
    assert nav.index == idx_before, "滚轮不应改变选中项：%s -> %s" % (idx_before, nav.index)
    return ("滚轮 3 格 → 视图滚动 %s→%s（像聊天记录一样下滑）；选中项保持 index=%s 不变"
            % (before_scroll, after_scroll, nav.index))


def test_wheel_moves_selection_when_enabled():
    """显式开启 wheel_moves_selection 时，保留旧行为：滚轮移动选中项。"""
    nav, rl = _new_nav(wheel_moves=True)
    viewport = rl.scroll.viewport()
    sb = rl.scroll.verticalScrollBar()
    before_scroll = sb.value()
    for _ in range(2):
        ev = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, -120),
                         Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
        QApplication.sendEvent(viewport, ev)
    assert nav.index == 2, "开启后两格滚轮应到 index=2，实际 %s" % nav.index
    return "wheel_moves_selection=True 时滚轮 2 格 → index=2（视图未滚动：%s→%s）" % (before_scroll, sb.value())


TESTS = [test_path_equivalence, test_confirm_same, test_mixed_path,
         test_boundaries, test_wheel_scrolls_not_selects,
         test_wheel_moves_selection_when_enabled]


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    print("=== CNKI-Hover C1 键鼠双通道导航验收 ===")
    passed = failed = 0
    for fn in TESTS:
        try:
            detail = fn()
            passed += 1
            print("[PASS] %s %s" % (fn.__name__, detail))
        except AssertionError as e:
            failed += 1
            print("[FAIL] %s %s" % (fn.__name__, e))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("[FAIL] %s %s: %s" % (fn.__name__, type(e).__name__, e))
    print("\n=== 总结: %d PASS, %d FAIL ===" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
