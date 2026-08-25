"""State directory resolution for the apiary GUI.

GUI state lives under ``<main-apiary>/.apiary/gui/apiary_gui[_<profile>]/``
post per-repo migration (was ``~/.claude/apiary_gui[_<profile>]/`` before
phase 5 deleted the global tree).

``APIARY_GUI_PROFILE=<name>`` re-roots all per-instance state under
``apiary_gui_<name>/`` instead of ``apiary_gui/``. This lets a "dev"
source build run alongside the main packaged build without their
tabs.json / sidebar_state.json / theme.json fighting each other. Mutex
naming and the window title both pick up the profile too.

Resolving ``<main-apiary>`` differs between source and frozen builds:

* **Source:** ``gui/`` lives at ``<checkout>/gui/``, so the checkout root
  is simply this module's grandparent.
* **Frozen (PyInstaller):** ``__file__`` points *inside* the bundle
  (``_internal/...``), which is wiped and regenerated on every rebuild —
  anchoring state there silently loses tabs/settings and strands pasted
  files. Instead we walk up from the exe (``sys.executable``) to find the
  apiary checkout the build sits in. If the build was shipped outside any
  checkout, we fall back to a per-user data dir so state still persists.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def profile() -> str:
    """Active profile name (env var ``APIARY_GUI_PROFILE``), or empty string."""
    return os.environ.get("APIARY_GUI_PROFILE", "").strip()


def _find_apiary_checkout(start: Path) -> Path | None:
    """Walk up from ``start`` to the apiary checkout root, or ``None``.

    The checkout root is the first ancestor containing both a ``.git`` entry
    and the ``gui`` package — distinctive enough not to false-match an
    unrelated parent repository the build might happen to be nested inside.
    """
    for d in (start, *start.parents):
        if (d / ".git").exists() and (d / "gui").is_dir():
            return d
    return None


def _user_data_base() -> Path:
    """Per-OS user data root, used only as a frozen-build fallback.

    Standard locations: ``%LOCALAPPDATA%`` on Windows, ``Application
    Support`` on macOS, and ``$XDG_DATA_HOME`` (default ``~/.local/share``)
    elsewhere. State then lands at ``<base>/.apiary/gui/...`` like any other.
    """
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA")
        return Path(root) if root else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def main_apiary() -> Path:
    """Resolve the main-apiary root that per-repo apiary state hangs off of.

    Public because it is the single source of truth for source-vs-frozen
    root resolution: ``repo_registry`` needs the same answer, and a second
    copy of the logic is exactly how the packaged build ended up reading
    ``<bundle>/.repos/registry.json`` (#T-2026-248).
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        found = _find_apiary_checkout(exe_dir)
        return found if found is not None else _user_data_base()
    return Path(__file__).resolve().parent.parent


def state_dir() -> Path:
    """Per-profile state directory under ``<main-apiary>/.apiary/gui/``."""
    p = profile()
    name = f"apiary_gui_{p}" if p else "apiary_gui"
    return main_apiary() / ".apiary" / "gui" / name


def mutex_name() -> str:
    """Per-profile single-instance mutex name (Win32 named mutex)."""
    p = profile()
    base = "Global\\apiary_gui_singleton_v1"
    return f"{base}_{p}" if p else base


def window_title() -> str:
    """Per-profile window title (empty default, or ``[<profile>]`` when profiled)."""
    p = profile()
    return f"[{p}]" if p else ""
