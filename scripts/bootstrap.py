#!/usr/bin/env python3
"""Idempotent bootstrap for a fresh claude-apiary clone.

Creates the per-project state directory under ~/.claude/projects/<key>/,
seeds empty notes.jsonl / learnings.jsonl / memory/MEMORY.md, sets the
auto-startup flag if missing, and verifies the runtime environment.

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

# Files scribe writes under ~/.claude/projects/<key>/. Used only by the
# legacy-state guard below to detect "needs migration first" scenarios.
SCRIBE_OWNED = ("notes.jsonl", "learnings.jsonl", "memory")

MIN_PYTHON = (3, 9)
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
AUTO_STARTUP_FLAG = CLAUDE_DIR / "auto-startup-enabled"
REQUIREMENTS_FILE = REPO_ROOT / "requirements.txt"


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


def _check_python_version(result: BootstrapResult) -> None:
    if sys.version_info < MIN_PYTHON:
        have = ".".join(str(x) for x in sys.version_info[:3])
        need = ".".join(str(x) for x in MIN_PYTHON)
        result.warnings.append(
            f"Python {have} detected; claude-apiary requires {need}+."
        )


def _check_requirements(result: BootstrapResult) -> None:
    """Soft check: parse requirements.txt and verify each top-level package imports."""
    if not REQUIREMENTS_FILE.is_file():
        return
    missing: list[str] = []
    for raw in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Extract bare package name from "pkg>=1.0,<2.0" / "pkg==1.0" / "pkg".
        name = ""
        for ch in line:
            if ch.isalnum() or ch in ("_", "-"):
                name += ch
            else:
                break
        if not name:
            continue
        import_name = name.replace("-", "_")
        try:
            __import__(import_name)
        except ImportError:
            missing.append(name)
    if missing:
        result.warnings.append(
            f"Missing Python packages: {', '.join(missing)}. "
            f"Run: pip install -r {REQUIREMENTS_FILE.name}"
        )


def _legacy_state_present(repo_dir: Path, new_key: str) -> Optional[Path]:
    """Return the legacy project dir iff it holds scribe state and would orphan.

    Detects the case where an existing machine has scribe state under the old
    cwd-derived key but no state under the new stable key yet. Bootstrap must
    refuse to seed empty files in this state — that would mask the real data
    and block migrate_project_key.py from running.
    """
    legacy_key = project_key_from_path(repo_dir)
    if legacy_key == new_key:
        return None
    legacy_dir = PROJECTS_DIR / legacy_key
    new_dir = PROJECTS_DIR / new_key
    legacy_has_state = any((legacy_dir / name).exists() for name in SCRIBE_OWNED)
    new_has_state = any((new_dir / name).exists() for name in SCRIBE_OWNED)
    if legacy_has_state and not new_has_state:
        return legacy_dir
    return None


def bootstrap(repo_dir: Path) -> BootstrapResult:
    """Run all bootstrap steps for repo_dir. Returns a result tally."""
    result = BootstrapResult()

    project_key = get_project_key(repo_dir)
    project_dir = PROJECTS_DIR / project_key

    legacy_dir = _legacy_state_present(repo_dir, project_key)
    if legacy_dir is not None:
        result.warnings.append(
            f"Legacy scribe state found at {legacy_dir} and no state at "
            f"{project_dir}. Run `python scripts/migrate_project_key.py` "
            f"first, then re-run bootstrap. Refusing to seed empty files."
        )
        return result

    _ensure_dir(CLAUDE_DIR, "~/.claude", result)
    _ensure_dir(PROJECTS_DIR, "~/.claude/projects", result)
    _ensure_dir(project_dir, f"~/.claude/projects/{project_key}", result)
    _ensure_empty_file(project_dir / "notes.jsonl", "notes.jsonl", result)
    _ensure_empty_file(project_dir / "learnings.jsonl", "learnings.jsonl", result)

    memory_dir = project_dir / "memory"
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


def _print_summary(result: BootstrapResult, project_key: str, quiet: bool) -> None:
    if quiet and not result.warnings:
        return
    print(f"project key: {project_key}")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print warnings and a project-key line if anything was created.",
    )
    args = parser.parse_args()

    result = bootstrap(REPO_ROOT)
    _print_summary(result, get_project_key(REPO_ROOT), quiet=args.quiet)
    return 1 if result.warnings else 0


if __name__ == "__main__":
    sys.exit(main())
