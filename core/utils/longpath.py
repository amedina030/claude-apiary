"""Windows long-path helpers.

Windows refuses paths longer than MAX_PATH (260 characters) unless they carry
the extended-length prefix ``\\\\?\\``. A git worktree with a virtualenv in it
routinely crosses that line, at which point ``git worktree remove`` fails
with 'Filename too long' after dropping its own bookkeeping, and the
directory is orphaned (T-2026-303, L-2026-170). Python's ``shutil.rmtree``
handles such trees fine when handed the prefixed absolute path.

On every other OS these helpers are pass-throughs, so callers need no
platform branching of their own.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

EXTENDED_PREFIX = "\\\\?\\"


def extended_path(path) -> str:
    """Absolute *path* with the extended-length prefix on Windows, unchanged elsewhere."""
    text = os.fspath(path)
    if os.name != "nt":
        return text
    if text.startswith(EXTENDED_PREFIX):
        return text
    absolute = str(Path(text).resolve())
    if absolute.startswith("\\\\"):
        # UNC share: \\server\share -> \\?\UNC\server\share
        return EXTENDED_PREFIX + "UNC" + absolute[1:]
    return EXTENDED_PREFIX + absolute


def rmtree_long(path, ignore_errors: bool = False) -> None:
    """``shutil.rmtree`` that survives over-long paths on Windows."""
    shutil.rmtree(extended_path(path), ignore_errors=ignore_errors)
