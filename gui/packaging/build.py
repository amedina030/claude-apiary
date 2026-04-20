"""One-folder PyInstaller build for the apiary GUI.

Run from the apiary repo root (poetry handles cwd):

    poetry run python gui/packaging/build.py

Outputs to ``dist/apiary-gui/`` next to the spec's repo root. Cleans the
previous build/ and dist/apiary-gui/ first so partial bundles don't linger.

This is a thin wrapper over PyInstaller — the actual build config lives in
``gui/packaging/apiary_gui.spec``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGING_DIR.parent.parent
SPEC = PACKAGING_DIR / "apiary_gui.spec"


def main() -> int:
    if not SPEC.is_file():
        print(f"spec missing: {SPEC}", file=sys.stderr)
        return 1

    build_dir = REPO_ROOT / "build"
    dist_dir = REPO_ROOT / "dist" / "apiary-gui"
    for stale in (build_dir, dist_dir):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
