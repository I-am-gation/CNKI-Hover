"""全局热键（Windows 原生 RegisterHotKey）。

设计要点：
- 用 `RegisterHotKey(None, id, mods, vk)` 把热键注册到**线程消息队列**（hwnd=NULL）。
- 独立监听线程跑 `GetMessageW` 循环，收到 `WM_HOTKEY` 后 emit Qt 信号
  （跨线程 emit → Qt 自动走 QueuedConnection 投递到主线程）。
  这样**不依赖 Qt 自己的消息泵**，与 Qt 事件循环解耦，最稳。
- 降级：若 `RegisterHotKey` 失败（热键被占用 / 无权限），`register()` 返回 False，
  上层可回退到备选序列或提示用户（见任务卡降级策略）。

B1 产出，供 overlay / settings（C5 热更新）调用。
"""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import QObject, Signal

from .log import get_logger, log_event

LOG = get_logger("hotkey")

# ---- Win32 常量 ----
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_HOTKEY_ID = 0xB001

_MODS = {
    "alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "shift": MOD_SHIFT, "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN,
}

# 常用虚拟键码表（够覆盖热键场景；未收录的可用 "VK:0xNN" 直接指定）
_VK = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
for _i in range(10):
    _VK[str(_i)] = 0x30 + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _VK[_c.lower()] = ord(_c)
for _n in range(1, 25):
    _VK["f%d" % _n] = 0x6F + _n  # F1=0x70


class HotkeyError(Exception):
    """热键串解析失败。"""


def parse_hotkey(sequence: str) -> tuple[int, int]:
    """把 "Alt+Space" / "Ctrl+Shift+K" 解析成 (modifiers, vk)。失败抛 HotkeyError。"""
    if not sequence or not sequence.strip():
        raise HotkeyError("热键为空")
    parts = [p.strip().lower() for p in sequence.replace("，", "+").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("热键为空")
    mods = 0
    vk = None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
            continue
        if p.startswith("vk:"):
            try:
                vk = int(p[3:], 0)
            except ValueError as e:
                raise HotkeyError("非法虚拟键码：%s" % p) from e
            continue
        if p in _VK:
            vk = _VK[p]
            continue
        if len(p) == 1:
            vk = ord(p.upper())
            continue
        raise HotkeyError("无法识别的按键：%s" % p)
    if vk is None:
        raise HotkeyError("热键缺少主键（只有修饰键）：%s" % sequence)
    if mods == 0:
        # 裸键注册会抢占全局输入，拒绝
        raise HotkeyError("热键必须包含至少一个修饰键（Ctrl/Alt/Shift/Win）：%s" % sequence)
    # 兼容旧写法
    if sys.platform == "win32":
        mods |= MOD_NOREPEAT
    return mods, vk


class GlobalHotkey(QObject):
    """全局热键。`activated` 在热键按下时发出（主线程）。"""

    activated = Signal()

    def __init__(self, sequence: str = "Alt+Space", parent: Optional[QObject] = None):
        super().__init__(parent)
        self._sequence = sequence
        self._mods = 0
        self._vk = 0
        self._registered = False
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._ok = False

    # ---- 属性 ----
    @property
    def sequence(self) -> str:
        return self._sequence

    @property
    def registered(self) -> bool:
        return self._registered

    # ---- 注册 / 注销 ----
    def register(self) -> bool:
        """注册热键。成功 True；失败 False（不抛，便于上层降级）。

        ⚠️ 关键点：`RegisterHotKey(NULL, id, ...)` 是把热键绑到**调用线程的消息队列**上，
        WM_HOTKEY 只会投递给「注册它的那个线程」。因此**注册与 GetMessage 必须同线程**——
        早期版本在主线程注册、在监听线程 GetMessage，真实按键永远收不到
        （当时验收走的是跨线程 emit 代理，正好把这个问题掩盖了）。
        """
        if sys.platform != "win32":
            log_event(LOG, "hotkey_register_skipped", reason="non-windows")
            return False
        self.unregister()
        try:
            mods, vk = parse_hotkey(self._sequence)
        except HotkeyError as e:
            log_event(LOG, "hotkey_parse_failed", sequence=self._sequence, error=str(e))
            return False

        self._mods, self._vk = mods, vk
        self._stop.clear()
        self._ready = threading.Event()
        self._ok = False
        self._thread = threading.Thread(target=self._pump, name="cnki-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)

        self._registered = bool(self._ok)
        if self._registered:
            log_event(LOG, "hotkey_registered", sequence=self._sequence, mods=mods, vk=vk,
                      tid=self._thread_id)
        else:
            log_event(LOG, "hotkey_register_failed", sequence=self._sequence)
        return self._registered

    def unregister(self) -> None:
        self._stop.set()
        if self._thread_id:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = None
        self._registered = False

    def update(self, sequence: str) -> bool:
        """热更新热键（C5 设置页用）：旧键立即失效，新键立即生效。"""
        old = self._sequence
        self.unregister()
        self._sequence = sequence
        ok = self.register()
        if not ok:
            # 回滚到旧键，保证功能不丢
            self._sequence = old
            self.register()
            log_event(LOG, "hotkey_update_rollback", requested=sequence, restored=old)
        log_event(LOG, "hotkey_updated", old=old, new=self._sequence, ok=ok)
        return ok

    # ---- 监听线程（注册与消息循环必须同线程，见 register() 注释） ----
    def _pump(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._thread_id = int(kernel32.GetCurrentThreadId())
        log_event(LOG, "hotkey_pump_started", tid=self._thread_id, sequence=self._sequence)

        if not user32.RegisterHotKey(None, _HOTKEY_ID, self._mods, self._vk):
            self._ok = False
            self._ready.set()
            log_event(LOG, "hotkey_register_failed", sequence=self._sequence,
                      winerror=ctypes.get_last_error(), tid=self._thread_id)
            return
        self._ok = True
        self._ready.set()

        msg = wintypes.MSG()
        while not self._stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                log_event(LOG, "hotkey_activated", sequence=self._sequence)
                self.activated.emit()  # 跨线程 emit → QueuedConnection 到主线程
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        try:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
        except Exception:  # noqa: BLE001
            pass
        log_event(LOG, "hotkey_pump_stopped", tid=self._thread_id)

    # ---- 供自动化验收：从独立线程触发，模拟真实热键路径 ----
    def simulate(self) -> None:
        """在独立线程里 emit `activated`，复现「热键来自其他线程」的真实链路。"""
        threading.Thread(target=self.activated.emit, name="cnki-hotkey-sim", daemon=True).start()
