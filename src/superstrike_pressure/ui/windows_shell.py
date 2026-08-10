"""Small Windows shell integrations used by the desktop application."""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Any
from pathlib import Path


def asset_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / name


class StartHotkeyListener:
    """Register Ctrl+F12 independently of focus while the panel runs."""

    HOTKEY_ID = 0x5354
    WM_HOTKEY = 0x0312
    PM_REMOVE = 0x0001
    MOD_CONTROL = 0x0002
    VK_F12 = 0x7B

    def __init__(self, callback: Any) -> None:
        self._callback = callback
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._registered = False
        self._thread = threading.Thread(
            target=self._run,
            name="superstrike-start-hotkey",
            daemon=True,
        )

    def start(self) -> bool:
        self._thread.start()
        self._ready.wait(timeout=1.0)
        return self._registered

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._registered = bool(
            user32.RegisterHotKey(
                None,
                self.HOTKEY_ID,
                self.MOD_CONTROL,
                self.VK_F12,
            )
        )
        self._ready.set()
        if not self._registered:
            return
        message = wintypes.MSG()
        try:
            while not self._stop.is_set():
                while user32.PeekMessageW(
                    ctypes.byref(message),
                    None,
                    0,
                    0,
                    self.PM_REMOVE,
                ):
                    if (
                        int(message.message) == self.WM_HOTKEY
                        and int(message.wParam) == self.HOTKEY_ID
                    ):
                        self._callback()
                        continue
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                time.sleep(0.01)
        finally:
            user32.UnregisterHotKey(None, self.HOTKEY_ID)
