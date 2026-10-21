"""Small helpers for frameless windows: rounded corners on Windows 11 and a draggable title strip."""

import ctypes
import os


def round_corners(win) -> None:
    """Asks Windows 11 for rounded window corners (no effect, and no error, elsewhere)."""
    if os.name != "nt":
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        value = ctypes.c_int(2)                                   # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(value), 4)
    except Exception:
        pass


def center(win, width, height) -> None:
    x = max(0, (win.winfo_screenwidth() - width) // 2)
    y = max(0, (win.winfo_screenheight() - height) // 2 - 20)
    win.geometry(f"{width}x{height}+{x}+{y}")


class Dragger:
    """Moves a frameless window by dragging: call `press(event)` and `move(event)` from mouse bindings."""

    def __init__(self, win):
        self.win = win
        self._dx = self._dy = 0

    def press(self, event) -> None:
        self._dx = event.x_root - self.win.winfo_x()
        self._dy = event.y_root - self.win.winfo_y()

    def move(self, event) -> None:
        self.win.geometry(f"+{event.x_root - self._dx}+{event.y_root - self._dy}")
