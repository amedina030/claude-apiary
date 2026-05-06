"""State directory resolution for the apiary GUI.

GUI state lives under ``<main-apiary>/.apiary/gui/apiary_gui[_<profile>]/``
post per-repo migration (was ``~/.claude/apiary_gui[_<profile>]/`` before
phase 5 deleted the global tree).

``APIARY_GUI_PROFILE=<name>`` re-roots all per-instance state under
``apiary_gui_<name>/`` instead of ``apiary_gui/``. This lets a "dev"
source build run alongside the main packaged build without their
tabs.json / sidebar_state.json / theme.json fighting each other. Mutex
naming and the window title both pick up the profile too.
"""

from __future__ import annotations

import os
from pathlib import Path

# main-apiary is the parent of this module's package — gui/ lives at
# <main-apiary>/gui/, so this resolution is robust whether the GUI is
# launched from a packaged build or directly from the source tree.
_MAIN_APIARY = Path(__file__).resolve().parent.parent


def profile() -> str:
    """Active profile name (env var ``APIARY_GUI_PROFILE``), or empty string."""
    return os.environ.get("APIARY_GUI_PROFILE", "").strip()


def state_dir() -> Path:
    """Per-profile state directory under ``<main-apiary>/.apiary/gui/``."""
    p = profile()
    name = f"apiary_gui_{p}" if p else "apiary_gui"
    return _MAIN_APIARY / ".apiary" / "gui" / name


def mutex_name() -> str:
    """Per-profile single-instance mutex name (Win32 named mutex)."""
    p = profile()
    base = "Global\\apiary_gui_singleton_v1"
    return f"{base}_{p}" if p else base


def window_title() -> str:
    """Per-profile window title (empty default, or ``[<profile>]`` when profiled)."""
    p = profile()
    return f"[{p}]" if p else ""
