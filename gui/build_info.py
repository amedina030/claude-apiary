"""Which commit did this build come from?

Nothing used to answer that. The PyInstaller manifest carries a hard-coded
``1.0.0.0``, the MCP server reported a hard-coded ``0.1.0``, and no CI builds
the exe — so two bundles from different commits were indistinguishable, and a
bug report against "the packaged GUI" could not be tied to a tree state
(review gui, "Build reproducibility — inputs pinned, process not").

The build writes ``gui/build_info.json`` into the bundle
(``gui/packaging/apiary_gui.spec`` calls :func:`write`), and the frozen app
reads it back. From source there is no bundle, so the commit is read from git
live — same answer, computed a different way.

Deliberately dependency-free and failure-tolerant: a build with no git, or an
exe copied out of its checkout, reports ``unknown`` rather than raising. A
provenance stamp must never be the reason a build or a launch fails.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Kept in step with `version` in pyproject.toml. The commit is what actually
# identifies a build; this is the human-facing series it belongs to.
BASE_VERSION = "0.1.0"
BUILD_INFO_NAME = "build_info.json"
UNKNOWN_COMMIT = ""
# Long enough to stay unambiguous in a repo this size, short enough to read.
COMMIT_LEN = 12

_cached: Optional[dict] = None


def repo_root() -> Path:
    """The checkout this module lives in (source runs only)."""
    return Path(__file__).resolve().parent.parent


def _run_git(args: list[str], cwd: Path) -> Optional[str]:
    """Run a git command, or return None if git is absent / the call fails.

    List-form, no shell, explicit encoding — a build must behave the same on
    a developer's Windows box and in CI on Linux.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_state(root: Optional[Path] = None) -> tuple[str, bool]:
    """``(short_commit, dirty)`` for *root*; ``("", False)`` when unknown.

    ``dirty`` means tracked files differ from HEAD — a stamp that claimed a
    commit while the tree had uncommitted edits would be worse than no stamp.
    """
    cwd = root or repo_root()
    if not cwd.is_dir():
        return UNKNOWN_COMMIT, False
    commit = _run_git(["rev-parse", f"--short={COMMIT_LEN}", "HEAD"], cwd)
    if not commit:
        return UNKNOWN_COMMIT, False
    status = _run_git(["status", "--porcelain", "--untracked-files=no"], cwd)
    # `status is None` = the call failed; don't claim clean on no evidence.
    return commit, bool(status) or status is None


def collect(root: Optional[Path] = None) -> dict:
    """Provenance for the tree at *root*, as written into the bundle."""
    commit, dirty = git_state(root)
    return {
        "version": BASE_VERSION,
        "commit": commit,
        "dirty": dirty,
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write(
    dest_dir: Path, root: Optional[Path] = None, info: Optional[dict] = None
) -> tuple[Path, dict]:
    """Write ``build_info.json`` into *dest_dir*; return ``(path, info)``.

    Returns what it wrote so a caller can report the stamp without a second
    git round-trip (which would also produce a second ``built_at``).
    """
    data = info if info is not None else collect(root)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / BUILD_INFO_NAME
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path, data


def bundled_path() -> Optional[Path]:
    """Path to the stamp inside a PyInstaller bundle, or None from source."""
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    return Path(meipass) / "gui" / BUILD_INFO_NAME


def load(refresh: bool = False) -> dict:
    """Provenance for the running app: the bundled stamp, else live git.

    Cached — a frozen build's answer cannot change, and from source this
    otherwise shells out to git on every call.
    """
    global _cached
    if _cached is not None and not refresh:
        return dict(_cached)
    info: Optional[dict] = None
    path = bundled_path()
    if path is not None:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                info = loaded
                info["origin"] = "bundle"
        except (OSError, ValueError):
            info = None
    if info is None:
        info = collect()
        info["origin"] = "git" if info.get("commit") else "unknown"
    info.setdefault("version", BASE_VERSION)
    info.setdefault("commit", UNKNOWN_COMMIT)
    info.setdefault("dirty", False)
    _cached = info
    return dict(info)


def version_string(info: Optional[dict] = None) -> str:
    """``0.1.0+g1a2b3c4d5e6f`` / ``…+g1a2b3c4d5e6f.dirty`` / ``0.1.0+unknown``.

    PEP 440 local-version shape, so it can be parsed as well as read.
    """
    data = info if info is not None else load()
    version = str(data.get("version") or BASE_VERSION)
    commit = str(data.get("commit") or "")
    if not commit:
        return f"{version}+unknown"
    return f"{version}+g{commit}" + (".dirty" if data.get("dirty") else "")
