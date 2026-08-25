#!/usr/bin/env python3
"""Install the secret-scan pre-commit hook into the CURRENT repo.

Sibling of ``scripts/install_repo_hooks.py``, which targets main-apiary's own
checkout and installs the combined doc-check + secret-scan hook. This one
targets whatever repo you run it from, so an apiary-managed side project gets
the same commit-time protection.

Run it through the per-repo launcher so it works from any managed repo::

    python .claude/apiary/launch.py scripts/install_git_hooks.py
    python .claude/apiary/launch.py scripts/install_git_hooks.py --uninstall
    python .claude/apiary/launch.py scripts/install_git_hooks.py --list

The incubator wires this into every newly spawned repo; run it by hand to
retrofit a repo that predates the feature.

Exit codes::
    0  success (or nothing to do)
    1  refused — a foreign pre-commit hook is in the way, or not a git repo
    2  bad arguments
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

HOOK_SOURCE = REPO_ROOT / "docs" / "hooks" / "pre-commit-secret-scan"

# Substring identifying a hook this installer owns. Both the per-repo hook and
# main-apiary's combined hook contain it, so we never clobber either.
OWNED_MARKER = "secret_scan.py"


def current_repo(start: Path) -> Path | None:
    """Git toplevel for *start*, or None when it isn't inside a work tree."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except (OSError, ValueError):
        return None
    out = proc.stdout.strip()
    return Path(out) if proc.returncode == 0 and out else None


def configured_hooks_path(repo: Path) -> str:
    """Value of ``core.hooksPath`` for *repo*, or "" when unset."""
    try:
        proc = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except (OSError, ValueError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def hooks_dir(repo: Path) -> tuple[Path, str | None]:
    """Where git will look for hooks in *repo*, plus a warning when redirected.

    ``core.hooksPath`` overrides ``.git/hooks`` entirely. Installing into
    ``.git/hooks`` while that is set produces a hook file git never runs — the
    installer reports success and the gate is silently dead. This repo shipped
    exactly that failure: a stale ``core.hooksPath`` left over from a directory
    rename pointed at a path that no longer existed, so every git hook here had
    been inert for months without a word.
    """
    configured = configured_hooks_path(repo)
    if not configured:
        return repo / ".git" / "hooks", None
    target = Path(configured)
    if not target.is_absolute():
        target = repo / target
    if not target.is_dir():
        return target, (
            f"core.hooksPath points at {target}, which does not exist — git runs "
            "NO hooks in this repo. Fix or clear it:\n"
            "    git config --unset core.hooksPath"
        )
    return target, (
        f"core.hooksPath redirects hooks to {target}; installing there rather "
        "than in .git/hooks."
    )


def hook_path(repo: Path) -> Path:
    return hooks_dir(repo)[0] / "pre-commit"


def _classify(target: Path) -> str:
    """One of: 'absent', 'ours', 'foreign'."""
    if not target.exists():
        return "absent"
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "foreign"
    return "ours" if OWNED_MARKER in content else "foreign"


def install(repo: Path, force: bool = False) -> int:
    target_dir, warning = hooks_dir(repo)
    if warning:
        print(f"  WARNING: {warning}")
    if not target_dir.is_dir():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"  refused: cannot create {target_dir}: {exc}")
            return 1

    if repo.resolve() == REPO_ROOT.resolve():
        print("  refused: this is main-apiary itself.")
        print("  Use: python scripts/install_repo_hooks.py")
        print("  (that installs the combined doc-check + secret-scan hook)")
        return 1

    if not HOOK_SOURCE.is_file():
        print(f"  refused: hook source missing: {HOOK_SOURCE}")
        return 1

    target = hook_path(repo)
    state = _classify(target)
    if state == "foreign" and not force:
        print(f"  refused: {target} exists and is not ours — leaving it alone.")
        print("  Inspect it, then re-run with --force to replace it.")
        return 1

    shutil.copy2(HOOK_SOURCE, target)
    target.chmod(target.stat().st_mode | 0o755)  # no-op on Windows
    verb = "replaced" if state != "absent" else "installed"
    print(f"  pre-commit hook  : {verb} at {target}")
    return 0


def uninstall(repo: Path) -> int:
    target = hook_path(repo)
    state = _classify(target)
    if state == "absent":
        print(f"  nothing to do: no hook at {target}")
        return 0
    if state == "foreign":
        print(f"  refused: {target} is not ours — leaving it alone.")
        return 1
    try:
        target.unlink()
    except OSError as exc:
        print(f"  error removing {target}: {exc}")
        return 1
    print(f"  removed: {target}")
    return 0


def report(repo: Path) -> int:
    target = hook_path(repo)
    _, warning = hooks_dir(repo)
    if warning:
        print(f"  WARNING: {warning}")
    state = _classify(target)
    label = {
        "absent": "not installed",
        "ours": "installed (apiary secret-scan)",
        "foreign": "present but NOT ours — install would refuse",
    }[state]
    print(f"  {target}: {label}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--uninstall", action="store_true", help="Remove the hook if we own it.")
    group.add_argument("--list", dest="list_mode", action="store_true", help="Report status only.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing non-apiary pre-commit hook.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Target repo (default: the git repo containing the working directory).",
    )
    args = parser.parse_args(argv)

    start = (args.repo or Path.cwd()).expanduser()
    repo = current_repo(start)
    if repo is None:
        print(f"error: {start} is not inside a git repository", file=sys.stderr)
        return 1

    print(f"Target repo: {repo}")
    if args.list_mode:
        return report(repo)
    if args.uninstall:
        return uninstall(repo)
    return install(repo, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
