"""Flip the native Win11 titlebar to dark mode via the DWM API.

Keeps the standard min/max/close buttons (familiar UX, snap layouts, etc.) but the
titlebar background/text use the dark Windows theme so it matches the app body.
No-op on non-Windows or if the API call fails.
"""

from __future__ import annotations

import sys
from typing import Optional

# DWMWINDOWATTRIBUTE values. The "20" name landed in Windows 10 build 19041; older
# builds (before that release) used "19" for the same flag. Try 20 first, fall back to 19.
_DWMWA_USE_IMMERSIVE_DARK_MODE_NEW = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19


def apply_dark_titlebar(hwnd: Optional[int]) -> bool:
    if sys.platform != "win32" or hwnd is None or hwnd == 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False
    try:
        dwmapi = ctypes.windll.dwmapi
    except (OSError, AttributeError):
        return False

    dwmapi.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long  # HRESULT

    use_dark = ctypes.c_int(1)
    for attr in (_DWMWA_USE_IMMERSIVE_DARK_MODE_NEW, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
        hr = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(attr),
            ctypes.byref(use_dark),
            ctypes.sizeof(use_dark),
        )
        if hr == 0:
            return True
    return False


def find_window_by_title(title: str) -> Optional[int]:
    """Return the HWND of a top-level window matching `title`, or None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = user32.FindWindowW(None, title)
    return int(hwnd) if hwnd else None
