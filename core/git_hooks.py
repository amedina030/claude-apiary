"""Repo-local git hook management for apiary-managed repos.

The commit-time secret scan (``scripts/secret_scan.py``) is delivered as a
``pre-commit`` hook. This module owns installing, removing, and reporting on
that hook; ``scripts/install_git_hooks.py`` is a thin CLI over it.

The logic lives under ``core/`` rather than ``scripts/`` because
``core.install`` calls it on every bootstrap. A one-time sweep of existing
repos decays the moment a new repo is registered — which is exactly what
happened: a repo bootstrapped half an hour after the retrofit had no hook at
all (#T-2026-261). Protection that only arrives when someone remembers to run
a script is the failure mode the scan was built to remove.

Not to be confused with:

* ``core/hooks/`` — Claude Code hooks (``settings.json`` entries), a different
  mechanism entirely.
* ``scripts/install_repo_hooks.py`` — main-apiary's OWN ``.git/hooks``, which
  chains doc-conformance and the secret scan. This module deliberately refuses
  to target main-apiary so the two never fight over ``pre-commit``.

"Which repo is this?" is ``core.utils.gitutil.git_root``; this module used to
carry its own ``current_repo`` copy of it (review X-3).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HOOK_SOURCE = REPO_ROOT / "docs" / "hooks" / "pre-commit-secret-scan"

# Substring identifying a hook we own. Both the per-repo hook and main-apiary's
# combined hook contain it, so we never clobber either.
OWNED_MARKER = "secret_scan.py"


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


def classify(target: Path) -> str:
    """One of: 'absent', 'ours', 'foreign'."""
    if not target.exists():
        return "absent"
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "foreign"
    return "ours" if OWNED_MARKER in content else "foreign"


# Backwards-compatible alias: the CLI and its tests referenced the private name
# before this module existed.
_classify = classify


def is_main_apiary(repo: Path) -> bool:
    return repo.resolve() == REPO_ROOT.resolve()


def install(repo: Path, force: bool = False, quiet: bool = False) -> int:
    """Install the secret-scan pre-commit hook into *repo*. Returns an exit code.

    ``quiet`` suppresses the per-step chatter so ``core.install`` can fold the
    outcome into its own report.
    """

    def say(msg: str) -> None:
        if not quiet:
            print(msg)

    target_dir, warning = hooks_dir(repo)
    if warning:
        print(f"  WARNING: {warning}")      # never silenced: a dead gate must be loud
    if not target_dir.is_dir():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            say(f"  refused: cannot create {target_dir}: {exc}")
            return 1

    if is_main_apiary(repo):
        say("  refused: this is main-apiary itself.")
        say("  Use: python scripts/install_repo_hooks.py")
        say("  (that installs the combined doc-check + secret-scan hook)")
        return 1

    if not HOOK_SOURCE.is_file():
        say(f"  refused: hook source missing: {HOOK_SOURCE}")
        return 1

    target = hook_path(repo)
    state = classify(target)
    if state == "foreign" and not force:
        say(f"  refused: {target} exists and is not ours — leaving it alone.")
        say("  Inspect it, then re-run with --force to replace it.")
        return 1

    shutil.copy2(HOOK_SOURCE, target)
    target.chmod(target.stat().st_mode | 0o755)  # no-op on Windows
    verb = "replaced" if state != "absent" else "installed"
    say(f"  pre-commit hook  : {verb} at {target}")
    return 0


def uninstall(repo: Path) -> int:
    target = hook_path(repo)
    state = classify(target)
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
    state = classify(target)
    label = {
        "absent": "not installed",
        "ours": "installed (apiary secret-scan)",
        "foreign": "present but NOT ours — install would refuse",
    }[state]
    print(f"  {target}: {label}")
    return 0
