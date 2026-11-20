"""全局快捷键（Windows ``RegisterHotKey``）。

窗口隐藏后就点不到按钮了，所以用系统级快捷键唤回：

* ``Ctrl+Alt+Q`` 显示 / 隐藏窗口
* ``Ctrl+Alt+S`` 开始 / 停止

实现要点：``RegisterHotKey(NULL, ...)`` 把热键挂在**线程**上，因此需要
一个自带消息循环的线程（这里是个 daemon 线程）；回调只往队列里塞事件，
真正的 UI 操作仍然发生在主线程。
"""

from __future__ import annotations

import contextlib
import ctypes
import threading
from ctypes import wintypes
from dataclasses import dataclass

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

VK_Q = 0x51
VK_S = 0x53


@dataclass(frozen=True)
class Binding:
    """一个热键：id 用来区分事件，name 只用于日志。"""

    id: int
    name: str
    modifiers: int
    key: int


TOGGLE_WINDOW = Binding(1, "Ctrl+Alt+Q", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_Q)
TOGGLE_RUN = Binding(2, "Ctrl+Alt+S", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_S)
DEFAULT_BINDINGS: tuple[Binding, ...] = (TOGGLE_WINDOW, TOGGLE_RUN)


class HotkeyManager:
    """在后台线程注册全局热键，触发时回调 ``on_trigger(binding_id)``。"""

    def __init__(self, on_trigger, bindings: tuple[Binding, ...] = DEFAULT_BINDINGS) -> None:
        self._on_trigger = on_trigger
        self._bindings = bindings
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._failures: list[str] = []
        self._active: list[Binding] = []

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(self._failures)

    @property
    def active(self) -> tuple[str, ...]:
        return tuple(binding.name for binding in self._active)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="cursor-qa-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def stop(self) -> None:
        thread_id = self._thread_id
        self._thread = None
        if thread_id is None:
            return
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)

    # ------------------------------------------------------------------ 线程体
    def _run(self) -> None:
        user32 = getattr(ctypes.windll, "user32", None)
        if user32 is None:
            self._ready.set()
            return

        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # 先建消息队列，再注册热键
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        for binding in self._bindings:
            try:
                ok = user32.RegisterHotKey(None, binding.id, binding.modifiers, binding.key)
            except (AttributeError, OSError):
                ok = 0
            if ok:
                self._active.append(binding)
            else:
                self._failures.append(binding.name)
        self._ready.set()

        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self._on_trigger(int(msg.wParam))
        finally:
            for binding in self._active:
                try:
                    user32.UnregisterHotKey(None, binding.id)
                except (AttributeError, OSError):
                    continue
            self._active.clear()
