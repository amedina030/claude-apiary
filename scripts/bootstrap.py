#!/usr/bin/env python3
"""Idempotent bootstrap for a fresh claude-apiary clone.

Resolves the apiary repo's per-target state directory under
``<apiary>/.repos/<name>-<id>/`` (post C-2026-46 centralization) by
calling the state resolver, lays out the scribe typed-year folder
skeleton (todos/, handoffs/, …, learnings/) via ScribeStore.ensure_layout,
seeds an empty memory/MEMORY.md, sets the auto-startup flag if missing,
and verifies the runtime environment.

The legacy in-repo umbrella at ``<repo>/.apiary/`` only holds the pointer
file the resolver writes during registration; bootstrap doesn't seed any
state there anymore.

Safe to run repeatedly — never clobbers existing data.

Usage:
    python scripts/bootstrap.py [--quiet]
"""
import argparse
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.utils.project import get_project_key, project_key_from_path  # noqa: E402
from core.utils.state import resolve_target_state_dir  # noqa: E402
from scribe.store import ScribeStore  # noqa: E402

# Markers for the pre-v2 flat-jsonl scribe layout under ~/.claude/projects/<key>/.
# Only used by the legacy-state guard below.
LEGACY_SCRIBE_MARKERS = ("notes.jsonl", "learnings.jsonl", "memory")

# Markers indicating the in-repo scribe state dir has already been seeded or
# migrated into the typed-year layout. "memory" is kept as a marker because it
# survives every layout change; "todos" is the first typed folder ScribeStore
# creates on ensure_layout().
REPO_SCRIBE_MARKERS = ("todos", "memory")

MIN_PYTHON = (3, 11)
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
AUTO_STARTUP_FLAG = CLAUDE_DIR / "auto-startup-enabled"
REQUIREMENTS_FILE = REPO_ROOT / "requirements.txt"

# Scribe state lives under <state-dir>/scribe/ where <state-dir> is
# resolved by the registry (post C-2026-46). The legacy umbrella
# <repo>/.apiary/ now only holds the pointer file written during
# registration.
SCRIBE_SUBDIR_NAME = "scribe"


class BootstrapResult:
    """Tally of what bootstrap did so the run can print a summary."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.existed: list[str] = []
        self.warnings: list[str] = []

    def created_or_existed(self, label: str, was_created: bool) -> None:
        (self.created if was_created else self.existed).append(label)


def _ensure_dir(path: Path, label: str, result: BootstrapResult) -> None:
    if path.is_dir():
        result.created_or_existed(label, was_created=False)
        return
    path.mkdir(parents=True, exist_ok=True)
    result.created_or_existed(label, was_created=True)


def _ensure_empty_file(path: Path, label: str, result: BootstrapResult) -> None:
    if path.exists():
        result.created_or_existed(label, was_created=False)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    result.created_or_existed(label, was_created=True)


def _ensure_text_file(path: Path, body: str, label: str, result: BootstrapResult) -> None:
    if path.exists():
        result.created_or_existed(label, was_created=False)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    result.created_or_existed(label, was_created=True)


def _ensure_typed_year_layout(scribe_dir: Path, result: BootstrapResult) -> None:
    """Lay out the typed-year folder skeleton via ScribeStore.

    ScribeStore.ensure_layout() is idempotent: it creates every type folder
    (todos/, handoffs/, …, learnings/), each year subfolder for the current
    year, their empty index.jsonl files, archive subfolders, and next_seq
    counter files. Already-populated folders are untouched.
    """
    already_had_todos = (scribe_dir / "todos").exists()
    ScribeStore(scribe_dir).ensure_layout()
    result.created_or_existed(
        "scribe typed-year layout", was_created=not already_had_todos
    )


def _check_python_version(result: BootstrapResult) -> None:
    if sys.version_info < MIN_PYTHON:
        have = ".".join(str(x) for x in sys.version_info[:3])
        need = ".".join(str(x) for x in MIN_PYTHON)
        result.warnings.append(
            f"Python {have} detected; claude-apiary requires {need}+."
        )


def _parse_dep_names_from_pyproject(path: Path) -> list[str]:
    """Extract package names from pyproject.toml dev dependencies.

    Parses the ``[tool.poetry.group.dev.dependencies]`` table using a
    minimal line-by-line scan (no third-party TOML library needed).
    """
    names: list[str] = []
    in_dev_deps = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_dev_deps = line == "[tool.poetry.group.dev.dependencies]"
            continue
        if in_dev_deps and "=" in line:
            name = line.split("=", 1)[0].strip()
            if name:
                names.append(name)
    return names


def _parse_dep_names_from_requirements(path: Path) -> list[str]:
    """Extract package names from a pip requirements.txt file."""
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = ""
        for ch in line:
            if ch.isalnum() or ch in ("_", "-"):
                name += ch
            else:
                break
        if name:
            names.append(name)
    return names


PYPROJECT_FILE = REPO_ROOT / "pyproject.toml"


def _check_requirements(result: BootstrapResult) -> None:
    """Soft check: verify each dependency package imports successfully.

    Prefers pyproject.toml (canonical) and falls back to requirements.txt.
    """
    if PYPROJECT_FILE.is_file():
        dep_names = _parse_dep_names_from_pyproject(PYPROJECT_FILE)
        install_hint = "poetry install"
    elif REQUIREMENTS_FILE.is_file():
        dep_names = _parse_dep_names_from_requirements(REQUIREMENTS_FILE)
        install_hint = f"pip install -r {REQUIREMENTS_FILE.name}"
    else:
        return
    missing: list[str] = []
    for name in dep_names:
        import_name = name.replace("-", "_")
        try:
            __import__(import_name)
        except ImportError:
            missing.append(name)
    if missing:
        result.warnings.append(
            f"Missing Python packages: {', '.join(missing)}. "
            f"Run: {install_hint}"
        )


def _unmigrated_legacy_state(repo_dir: Path, scribe_dir: Path) -> Optional[Path]:
    """Return the legacy project dir iff it holds scribe state that wasn't
    migrated into the centralized scribe location yet.

    Detects the case where an existing machine has scribe state at
    ~/.claude/projects/<key>/{notes,learnings,memory} but the centralized
    ``<state-dir>/scribe/`` is empty. Bootstrap must refuse to seed in
    this state — writing empty files there would mask the legacy data
    and leave the user's real notes stranded.

    Returns ``None`` when either:
      - no legacy state exists, OR
      - the centralized location already has state (migration is done
        or in progress).
    """
    project_key = get_project_key(repo_dir)
    candidate_keys = {project_key, project_key_from_path(repo_dir)}
    scribe_has_state = any((scribe_dir / name).exists() for name in REPO_SCRIBE_MARKERS)
    if scribe_has_state:
        return None
    for key in candidate_keys:
        legacy_dir = PROJECTS_DIR / key
        if any((legacy_dir / name).exists() for name in LEGACY_SCRIBE_MARKERS):
            return legacy_dir
    return None


def _resolve_scribe_dir(repo_dir: Path) -> Path:
    """Return ``<state-dir>/scribe/`` for *repo_dir*, registering it if needed.

    The registry resolver auto-allocates an id, creates the per-target
    state dir under ``<apiary>/.repos/<name>-<id>/``, and writes a
    pointer file back into ``<repo_dir>/.apiary/`` on first call.
    """
    state_dir = resolve_target_state_dir(cwd=repo_dir, apiary_repo=repo_dir)
    return state_dir / SCRIBE_SUBDIR_NAME


def bootstrap(repo_dir: Path) -> BootstrapResult:
    """Run all bootstrap steps for repo_dir. Returns a result tally."""
    result = BootstrapResult()

    scribe_dir = _resolve_scribe_dir(repo_dir)

    legacy_dir = _unmigrated_legacy_state(repo_dir, scribe_dir)
    if legacy_dir is not None:
        result.warnings.append(
            f"Legacy scribe state found at {legacy_dir} and no state at "
            f"{scribe_dir}. Run `python scripts/migrate_scribe_state.py "
            f"--source {legacy_dir}` first, then re-run bootstrap. Refusing "
            f"to seed empty files."
        )
        return result

    _ensure_dir(CLAUDE_DIR, "~/.claude", result)
    _ensure_dir(scribe_dir, "scribe state dir", result)
    _ensure_typed_year_layout(scribe_dir, result)

    memory_dir = scribe_dir / "memory"
    _ensure_dir(memory_dir, "memory/", result)
    _ensure_text_file(memory_dir / "MEMORY.md", "", "memory/MEMORY.md", result)

    if AUTO_STARTUP_FLAG.exists():
        result.created_or_existed("auto-startup flag", was_created=False)
    else:
        AUTO_STARTUP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        AUTO_STARTUP_FLAG.write_text("enabled", encoding="utf-8")
        result.created_or_existed("auto-startup flag", was_created=True)

    _check_python_version(result)
    _check_requirements(result)

    return result


def _print_summary(result: BootstrapResult, repo_dir: Path, quiet: bool) -> None:
    if quiet and not result.warnings:
        return
    print(f"repo: {repo_dir}")
    print(f"scribe state: {_resolve_scribe_dir(repo_dir)}")
    if result.created:
        print("created:")
        for item in result.created:
            print(f"  + {item}")
    if result.existed and not quiet:
        print("already present:")
        for item in result.existed:
            print(f"  . {item}")
    if result.warnings:
        print("warnings:")
        for w in result.warnings:
            print(f"  ! {w}")


def _prompt_context_rules_install(quiet: bool, assume_yes: bool) -> None:
    """Final bootstrap phase: offer to install apiary context-rules into
    ~/.claude/CLAUDE.md. Idempotent — if everything is already installed and
    in sync, this is a no-op (silent under --quiet).
    """
    try:
        from core import context_rules as cr  # noqa: WPS433 (lazy import is intentional)
    except Exception:
        return

    try:
        rules = cr.load_all_rules()
    except Exception:
        return
    if not rules:
        return

    claude_md = CLAUDE_DIR / "CLAUDE.md"
    text = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""

    try:
        report = cr.compute_drift(text, rules)
    except Exception:
        return

    if not report.not_installed and report.clean:
        if not quiet:
            print("context-rules: already installed and in sync")
        return

    print()
    print("context-rules available for install into ~/.claude/CLAUDE.md:")
    for rid in report.not_installed:
        print(f"  + {rid}")
    if report.hash_mismatch:
        print(f"  ~ {len(report.hash_mismatch)} rule(s) out of date")
    if report.tampered_bodies or report.stray_text:
        print("  ! managed zone has manual edits — install will refuse without --force")

    if assume_yes:
        answer = "y"
    else:
        try:
            answer = input("install all? [Y/n]: ").strip().lower() or "y"
        except EOFError:
            answer = "n"
    if answer != "y":
        print("skipped context-rules install")
        return

    import subprocess
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "install_context_rules.py"), "--install-all"]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"context-rules install failed (exit {e.returncode})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print warnings and a project-key line if anything was created.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Assume 'yes' to all prompts (e.g. context-rules install).",
    )
    parser.add_argument(
        "--no-context-rules",
        action="store_true",
        help="Skip the context-rules install prompt.",
    )
    args = parser.parse_args()

    result = bootstrap(REPO_ROOT)
    _print_summary(result, REPO_ROOT, quiet=args.quiet)

    if not args.no_context_rules:
        _prompt_context_rules_install(quiet=args.quiet, assume_yes=args.yes)

    _run_cron_health_check(quiet=args.quiet)

    return 1 if result.warnings else 0


def _run_cron_health_check(*, quiet: bool) -> None:
    """Informational cron-health report at the tail end of bootstrap.

    Calls ``runner.cron_health.run_bootstrap_check`` which prints the
    current scheduled-entry drift status (if any registry entries exist).
    Never propagates failure — bootstrap's exit code intentionally does
    not depend on whether the host's scheduled tasks happen to match
    the registry.
    """
    try:
        from runner import cron_health  # noqa: WPS433 (lazy import)
    except Exception:
        return
    try:
        cron_health.run_bootstrap_check(stream=sys.stdout)
    except Exception as exc:
        if not quiet:
            print(f"cron_health check skipped: {exc}")


if __name__ == "__main__":
    sys.exit(main())
