"""Single-instance enforcement via Windows named mutex.

Returns a context-manager-friendly object: if another instance holds the mutex
already, `acquired` is False — caller should try to foreground the existing
window (POST a custom message via FindWindow) and exit.

V1 just exits without the foreground-bump; the foreground hop adds Win32 boilerplate
that's not load-bearing for the "don't double-launch" requirement.
"""

from __future__ import annotations

import sys


_DEFAULT_NAME = "Global\\apiary_gui_singleton_v1"


class SingleInstance:
    def __init__(self, name: str = _DEFAULT_NAME) -> None:
        self.name = name
        self.acquired = False
        self._handle = None

    def __enter__(self) -> "SingleInstance":
        if sys.platform != "win32":
            self.acquired = True
            return self
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            self.acquired = True
            return self

        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183

        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateMutexW(None, False, self.name)
        last_err = kernel32.GetLastError()
        if not handle:
            self.acquired = True  # fail-open
            return self
        if last_err == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            self.acquired = False
            return self
        self._handle = handle
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._handle)
        except Exception:
            pass
        self._handle = None
