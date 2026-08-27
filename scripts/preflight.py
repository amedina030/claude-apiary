#!/usr/bin/env python3
"""Pre-install environment check for claude-apiary.

Runs BEFORE `poetry install` so a fresh machine learns about every missing
or fragile prerequisite at once, instead of discovering them one cryptic
failure at a time. Invoked by scripts/install.ps1 and scripts/install.sh with
the already-discovered interpreter; also runnable by hand:

    python scripts/preflight.py            # base install checks
    python scripts/preflight.py --gui      # also check the desktop GUI prereqs

Stdlib only, and deliberately imports nothing from apiary itself, so it works
on a bare clone whose dependencies are not installed yet.

Exit code: 0 if there are no hard blockers (FAILs); 1 if any FAIL is present.
WARN/INFO never change the exit code — the install can proceed past them.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from pathlib import Path

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"

_MARK = {OK: "  OK  ", WARN: "  !   ", FAIL: "  X   ", INFO: "  ->  "}

# pythonnet (a GUI dependency) only ships wheels for these versions.
GUI_PY_MAX_EXCLUSIVE = (3, 13)
GUI_PY_MIN = (3, 11)

WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"


class Result:
    """One check outcome: a status, a headline, and an optional fix line."""

    def __init__(self, status: str, title: str, fix: str | None = None) -> None:
        self.status = status
        self.title = title
        self.fix = fix


def check_python() -> Result:
    v = sys.version_info[:3]
    pretty = ".".join(str(x) for x in v)
    if v[:2] < GUI_PY_MIN:
        return Result(
            FAIL,
            f"Python {pretty} is too old (need >= 3.11).",
            "Install Python 3.11+ and re-run.",
        )
    return Result(OK, f"Python {pretty} ({sys.executable})")


def _gui_python_status(version: tuple[int, int]) -> str:
    """Pure GUI-compatibility verdict for a (major, minor) interpreter tuple."""
    if version >= GUI_PY_MAX_EXCLUSIVE:
        return WARN
    if version < GUI_PY_MIN:
        return FAIL
    return OK


def check_gui_python() -> Result:
    pretty = ".".join(str(x) for x in sys.version_info[:3])
    status = _gui_python_status(sys.version_info[:2])
    if status == WARN:
        return Result(
            WARN,
            f"Python {pretty} is too new for the GUI's pythonnet (needs < 3.13).",
            "The GUI window will fail to open on this interpreter. Install a "
            "3.12 and run `poetry env use <python3.12>` before `--with gui`.",
        )
    if status == FAIL:
        return Result(FAIL, f"Python {pretty} is too old for the GUI (need 3.11 or 3.12).")
    return Result(OK, f"Python {pretty} is GUI-compatible (3.11/3.12)")


def check_git() -> Result:
    if shutil.which("git"):
        return Result(OK, "git is on PATH")
    return Result(
        FAIL,
        "git not found — apiary identifies every target repo via git.",
        "Install git (https://git-scm.com/downloads) and re-run.",
    )


def _path_flags(home: str, here: str) -> list[str]:
    """Return human-readable flags for fragile characters in the given paths.

    Pure so it can be tested without touching the real environment.
    """
    flagged = []
    for label, value in (("home dir", home), ("repo path", here)):
        if "'" in value:
            flagged.append(f"{label} contains an apostrophe ({value})")
        elif " " in value:
            flagged.append(f"{label} contains a space ({value})")
        elif any(ord(ch) > 127 for ch in value):
            flagged.append(f"{label} contains a non-ASCII character ({value})")
    return flagged


def check_path_sanity() -> Result:
    """Warn (do not block) when the install path or home dir holds characters
    that have historically broken shells/tools — a space, an apostrophe, or a
    non-ASCII letter. Apiary itself is hardened for these, but third-party
    tooling in the chain (Poetry, pty backends) is less predictable."""
    flagged = _path_flags(str(Path.home()), str(Path(__file__).resolve()))
    if not flagged:
        return Result(OK, "install path is free of spaces/apostrophes/non-ASCII")
    return Result(
        WARN,
        "; ".join(flagged) + ".",
        "Apiary supports this, but if Poetry or the GUI pty misbehave, a path "
        "without these characters is the safest fallback.",
    )


def _is_shim(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (".cmd", ".bat", ".ps1")


def check_claude(for_gui: bool) -> Result:
    found = shutil.which("claude")
    if not found:
        # Not fatal for a base install (you may run apiary via poetry), but the
        # GUI must be able to spawn it.
        status = WARN if for_gui else INFO
        return Result(
            status,
            "the `claude` CLI was not found on PATH.",
            "Install Claude Code so the GUI can launch a session (https://claude.ai/claude-code).",
        )
    if os.name == "nt" and _is_shim(found):
        return Result(
            INFO,
            f"`claude` resolves to a shim ({found}).",
            "The GUI wraps batch shims through cmd.exe automatically; if a real "
            "claude.exe exists it is preferred.",
        )
    return Result(OK, f"`claude` found ({found})")


def _webview2_installed() -> bool:
    """True if the Edge WebView2 Evergreen runtime is registered (Windows)."""
    if os.name != "nt":
        return False
    try:
        import winreg  # stdlib, Windows only
    except ImportError:
        return False
    client = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    locations = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\" + client,
        ),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + client),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\\" + client),
    ]
    for hive, subkey in locations:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                version, _ = winreg.QueryValueEx(k, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def check_webview2() -> Result:
    if os.name != "nt":
        return Result(INFO, "WebView2 check skipped (non-Windows host)")
    if _webview2_installed():
        return Result(OK, "Edge WebView2 runtime is installed")
    return Result(
        WARN,
        "Edge WebView2 runtime was not detected — the GUI renders through it.",
        f"Install the Evergreen runtime: {WEBVIEW2_DOWNLOAD}",
    )


def run(for_gui: bool) -> int:
    print(
        f"preflight: {platform.system()} {platform.release()}, "
        f"python {'.'.join(str(x) for x in sys.version_info[:3])}"
        f"{' (+gui)' if for_gui else ''}"
    )
    print()

    results = [check_python(), check_git(), check_path_sanity(), check_claude(for_gui)]
    if for_gui:
        results += [check_gui_python(), check_webview2()]

    had_fail = False
    for r in results:
        print(f"{_MARK[r.status]}{r.title}")
        if r.fix and r.status in (WARN, FAIL):
            print(f"        -> {r.fix}")
        had_fail = had_fail or r.status == FAIL

    print()
    if had_fail:
        print("preflight: blockers above must be fixed before install can proceed.")
        return 1
    print("preflight: clear to proceed (review any warnings above).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-install environment check for claude-apiary.")
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Also check desktop GUI prerequisites (pythonnet Python pin, WebView2).",
    )
    args = parser.parse_args()
    return run(for_gui=args.gui)


if __name__ == "__main__":
    raise SystemExit(main())
