"""Taskbar flash (FlashWindowEx) for 'Claude awaits your input' signalling.

Called from GuiBridge when an assistant turn ends or an interactive prompt
appears while the window is minimized or not foreground. No-op on non-
Windows, and no-op when the window is already foreground (so normal active
use never triggers a flash).
"""

from __future__ import annotations

import sys
from typing import Optional


def _user32():
    import ctypes
    return ctypes.windll.user32


def _is_foreground(hwnd: int) -> bool:
    try:
        return int(_user32().GetForegroundWindow()) == int(hwnd)
    except Exception:
        # Fail-safe: treat as foreground so we don't spam flashes on an error.
        return True


def _is_minimized(hwnd: int) -> bool:
    try:
        return bool(_user32().IsIconic(hwnd))
    except Exception:
        return False


def flash_taskbar(hwnd: Optional[int]) -> bool:
    """Flash our taskbar button until the user foregrounds the window.

    Returns True if a flash was issued, False otherwise (non-Windows, no
    hwnd, already foreground and not minimized, or a Win32 call failed).
    """
    if sys.platform != "win32" or hwnd is None or hwnd == 0:
        return False
    if not _is_minimized(hwnd) and _is_foreground(hwnd):
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("hwnd", wintypes.HWND),
            ("dwFlags", wintypes.DWORD),
            ("uCount", wintypes.UINT),
            ("dwTimeout", wintypes.DWORD),
        ]

    FLASHW_TRAY = 0x00000002
    FLASHW_TIMERNOFG = 0x0000000C  # flash continuously until foreground

    try:
        user32 = _user32()
        user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
        user32.FlashWindowEx.restype = wintypes.BOOL
    except (OSError, AttributeError):
        return False

    info = FLASHWINFO()
    info.cbSize = ctypes.sizeof(FLASHWINFO)
    info.hwnd = wintypes.HWND(hwnd)
    info.dwFlags = FLASHW_TRAY | FLASHW_TIMERNOFG
    info.uCount = 0
    info.dwTimeout = 0
    try:
        return bool(user32.FlashWindowEx(ctypes.byref(info)))
    except Exception:
        return False
