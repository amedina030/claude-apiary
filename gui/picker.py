"""Folder-picker backend — directory listing + recents for the in-app picker modal.

The GUI picker is a custom themed modal (see web/app.js openPickerModal); this
module supplies the data it navigates. No native OS dialog is ever opened.
"""

from __future__ import annotations

import string
import sys
from pathlib import Path
from typing import Optional

from gui import repo_registry


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _windows_drives() -> list[dict]:
    """Enumerate existing drive letters as picker entries."""
    out: list[dict] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        if root.exists():
            out.append({"name": f"{letter}:\\", "path": str(root)})
    return out


def _normalize(path_str: Optional[str]) -> Optional[Path]:
    if not isinstance(path_str, str) or not path_str.strip():
        return None
    try:
        p = Path(path_str.strip()).expanduser()
    except (OSError, ValueError):
        return None
    try:
        return p.resolve()
    except OSError:
        return p


def list_directory(path_str: Optional[str]) -> dict:
    """Return a JSON-serializable description of `path_str`'s contents.

    On Windows, an empty/None path returns the "computer" view (drives).
    On POSIX, an empty/None path returns the home directory.
    Errors are surfaced via the `error` field rather than raised.
    """
    p = _normalize(path_str)
    if p is None:
        if _is_windows():
            return {
                "path": "",
                "display": "Computer",
                "is_root": True,
                "parent": None,
                "entries": _windows_drives(),
                "error": None,
            }
        p = Path.home()

    if not p.exists():
        return {
            "path": str(p),
            "display": str(p),
            "is_root": False,
            "parent": str(p.parent) if p.parent != p else None,
            "entries": [],
            "error": f"path does not exist: {p}",
        }
    if not p.is_dir():
        return {
            "path": str(p),
            "display": str(p),
            "is_root": False,
            "parent": str(p.parent) if p.parent != p else None,
            "entries": [],
            "error": f"not a directory: {p}",
        }

    entries: list[dict] = []
    error: Optional[str] = None
    try:
        for child in p.iterdir():
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except PermissionError as e:
        error = f"permission denied: {e}"
    except OSError as e:
        error = f"cannot list: {e}"

    entries.sort(key=lambda e: e["name"].casefold())

    parent: Optional[str]
    if _is_windows() and p.parent == p:
        parent = ""
    elif p.parent == p:
        parent = None
    else:
        parent = str(p.parent)

    return {
        "path": str(p),
        "display": str(p),
        "is_root": False,
        "parent": parent,
        "entries": entries,
        "error": error,
    }


def picker_context() -> dict:
    """Return recent repos, home dir, and the initial path the modal should land on."""
    repos, _err = repo_registry.load()
    home = str(Path.home())
    initial: str
    if repos:
        initial = str(repos[0].parent) if repos[0].parent != repos[0] else str(repos[0])
    else:
        initial = home
    return {
        "recents": [str(p) for p in repos],
        "home": home,
        "initial": initial,
    }
