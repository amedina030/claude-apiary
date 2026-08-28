"""One-folder PyInstaller build for the apiary GUI.

PyInstaller is a build-only dependency, pinned in the ``build`` poetry group:

    poetry install --with gui --with build
    poetry run python gui/packaging/build.py

Outputs to ``dist/apiary-gui/`` next to the spec's repo root. Cleans the
previous build/ and dist/apiary-gui/ first so partial bundles don't linger.

This is a thin wrapper over PyInstaller — the actual build config lives in
``gui/packaging/apiary_gui.spec``, which also stamps the commit being built
into ``<bundle>/_internal/gui/build_info.json`` (see gui/build_info.py).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGING_DIR.parent.parent
SPEC = PACKAGING_DIR / "apiary_gui.spec"
BUILD_INFO_REL = Path("_internal") / "gui" / "build_info.json"


def _running_gui_pids() -> list[int]:
    """Return PIDs of any running apiary-gui.exe (Windows only; [] elsewhere).

    A running exe holds handles on bundled DLLs (ClrLoader.dll, WebView2Loader.dll,
    python3X.dll, apiary-gui.exe), so pre-cleaning and PyInstaller both silently
    misbehave. Block the build rather than emit a frankenbuild.
    """
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq apiary-gui.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2 or not parts[0].lower().startswith("apiary-gui"):
            continue
        try:
            pids.append(int(parts[1]))
        except ValueError:
            continue
    return pids


def _pyinstaller_missing() -> bool:
    return importlib.util.find_spec("PyInstaller") is None


def _report_stamp(dist_dir: Path) -> None:
    """Print the provenance the spec stamped into the bundle.

    Best-effort: the build already succeeded by the time this runs, so a
    missing or unreadable stamp is worth a note, not a failure.
    """
    path = dist_dir / BUILD_INFO_REL
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"warning: no build stamp at {path}", file=sys.stderr)
        return
    commit = info.get("commit") or "unknown"
    dirty = " (dirty tree)" if info.get("dirty") else ""
    print(f"stamped: {info.get('version', '?')} @ {commit}{dirty}")


def main() -> int:
    if not SPEC.is_file():
        print(f"spec missing: {SPEC}", file=sys.stderr)
        return 1

    if _pyinstaller_missing():
        print(
            "error: PyInstaller is not installed in this environment. "
            "Run `poetry install --with gui --with build`.",
            file=sys.stderr,
        )
        return 1

    pids = _running_gui_pids()
    if pids:
        pid_list = ", ".join(str(p) for p in pids)
        print(
            f"error: apiary-gui.exe is running (PIDs: {pid_list}) — close it before rebuilding",
            file=sys.stderr,
        )
        return 1

    build_dir = REPO_ROOT / "build"
    dist_dir = REPO_ROOT / "dist" / "apiary-gui"
    for stale in (build_dir, dist_dir):
        if not stale.exists():
            continue
        try:
            shutil.rmtree(stale)
        except (PermissionError, OSError) as exc:
            print(
                f"error: failed to clean {stale}: {exc}. "
                "A process may still hold handles on bundled DLLs.",
                file=sys.stderr,
            )
            return 1

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(REPO_ROOT / "dist"),
        "--workpath",
        str(build_dir),
        str(SPEC),
    ]
    print("running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
    if result.returncode != 0:
        return result.returncode

    exe = dist_dir / "apiary-gui.exe"
    if not exe.is_file():
        print(f"expected exe missing after build: {exe}", file=sys.stderr)
        return 1

    print(f"built: {exe}")
    _report_stamp(dist_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
