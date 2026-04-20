"""State directory resolution for the apiary GUI.

``APIARY_GUI_PROFILE=<name>`` re-roots all per-instance state under
``~/.claude/apiary_gui_<name>/`` instead of ``~/.claude/apiary_gui/``. This
lets a "dev" source build run alongside the main packaged build without
their tabs.json / sidebar_state.json / theme.json fighting each other.

Mutex naming and the window title both pick up the profile too, so the
two instances are also distinguishable to Windows and to the user.
"""

from __future__ import annotations

import os
from pathlib import Path


def profile() -> str:
    """Active profile name (env var ``APIARY_GUI_PROFILE``), or empty string."""
    return os.environ.get("APIARY_GUI_PROFILE", "").strip()


def state_dir() -> Path:
    """Per-profile state directory (``~/.claude/apiary_gui[_<profile>]``)."""
    p = profile()
    name = f"apiary_gui_{p}" if p else "apiary_gui"
    return Path.home() / ".claude" / name


def mutex_name() -> str:
    """Per-profile single-instance mutex name (Win32 named mutex)."""
    p = profile()
    base = "Global\\apiary_gui_singleton_v1"
    return f"{base}_{p}" if p else base


def window_title() -> str:
    """Per-profile window title (``apiary`` or ``apiary [<profile>]``)."""
    p = profile()
    return f"apiary [{p}]" if p else "apiary"
