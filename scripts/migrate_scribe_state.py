#!/usr/bin/env python3
"""One-shot migration: ~/.claude/projects/claude-apiary/ -> <repo>/.apiary/scribe/

Copies scribe's operational state out of the shared ~/.claude/projects/<key>/
directory (legacy layout) and into an umbrella ``<repo>/.apiary/scribe/``
directory so the state travels with the repo checkout. Part of the in-repo
state migration decided 2026-04-09 (decision #269, todo #266).

**The script copies, it does not move.** The old location is left intact so
you can verify the new layout works (``APIARY_STATE_LAYOUT=repo python -m
scribe.notes list``) before cleaning up the legacy files. Legacy cleanup
lives in a separate todo (#268).

Files migrated:
  - notes.jsonl
  - notes_archive.jsonl
  - learnings.jsonl
  - memory/                 (recursive)

Files skipped:
  - *.lock                  (FileLock siblings; regenerated on next write)

Usage:
    python scripts/migrate_scribe_state.py [--dry-run] [--force] [--source DIR] [--target DIR]

By default:
  - source = ~/.claude/projects/claude-apiary/
  - target = <git-repo-root(cwd)>/.apiary/scribe/

The script is idempotent: re-running without --force refuses to overwrite
existing target files and reports what would have been copied. Pass --force
to clobber the target (useful when re-migrating after adding new notes).
"""
import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scribe.notes import _git_repo_root  # noqa: E402

LEGACY_DEFAULT = Path.home() / ".claude" / "projects" / "claude-apiary"
STATE_FILES = (
    "notes.jsonl",
    "notes_archive.jsonl",
    "learnings.jsonl",
)
SKIP_SUFFIXES = (".lock",)


def _default_target() -> Path | None:
    """Resolve the default target under <git-repo-root(cwd)>/.apiary/scribe/."""
    root = _git_repo_root()
    if root is None:
        return None
    return root / ".apiary" / "scribe"


def _ensure_umbrella_gitignore(umbrella: Path) -> bool:
    """Write .apiary/.gitignore containing '*' if it doesn't already exist.

    Returns True when a file was created, False when one was already there.
    """
    umbrella.mkdir(parents=True, exist_ok=True)
    gitignore = umbrella / ".gitignore"
    if gitignore.exists():
        return False
    gitignore.write_text("*\n", encoding="utf-8")
    return True


def _should_skip(path: Path) -> bool:
    return any(path.name.endswith(suf) for suf in SKIP_SUFFIXES)


def _copy_file(src: Path, dst: Path, *, force: bool, dry_run: bool) -> str:
    """Copy a single file. Returns a status word for the report."""
    if not src.is_file():
        return "missing"
    if dst.exists() and not force:
        return "exists"
    if dry_run:
        return "would-copy"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return "copied"


def _copy_memory_dir(src: Path, dst: Path, *, force: bool, dry_run: bool) -> dict:
    """Recursively copy memory/ contents. Returns a {status: count} tally.

    Skips any *.lock sibling files. Refuses to overwrite existing target
    files unless *force* is True.
    """
    tally = {"copied": 0, "exists": 0, "would-copy": 0, "skipped": 0}
    if not src.is_dir():
        return tally
    for entry in src.rglob("*"):
        if entry.is_dir():
            continue
        if _should_skip(entry):
            tally["skipped"] += 1
            continue
        rel = entry.relative_to(src)
        target = dst / rel
        status = _copy_file(entry, target, force=force, dry_run=dry_run)
        tally[status] = tally.get(status, 0) + 1
    return tally


def migrate(source: Path, target: Path, *, force: bool = False, dry_run: bool = False) -> dict:
    """Run the migration and return a structured report dict.

    Caller is responsible for printing the report. The function itself
    is silent so tests can assert on the return value without capturing
    stdout.
    """
    report = {
        "source": str(source),
        "target": str(target),
        "dry_run": dry_run,
        "force": force,
        "files": {},
        "memory": {},
        "umbrella_gitignore_created": False,
        "source_exists": source.is_dir(),
    }

    if not source.is_dir():
        return report

    umbrella = target.parent  # .apiary/
    if dry_run:
        # Don't create dirs during dry-run — only report what would happen.
        report["umbrella_gitignore_created"] = not (umbrella / ".gitignore").exists()
    else:
        report["umbrella_gitignore_created"] = _ensure_umbrella_gitignore(umbrella)
        target.mkdir(parents=True, exist_ok=True)

    for name in STATE_FILES:
        report["files"][name] = _copy_file(
            source / name, target / name, force=force, dry_run=dry_run
        )

    report["memory"] = _copy_memory_dir(
        source / "memory", target / "memory", force=force, dry_run=dry_run
    )

    return report


def _print_report(report: dict) -> None:
    dry = " (DRY RUN)" if report["dry_run"] else ""
    print(f"Scribe state migration{dry}")
    print(f"  source: {report['source']}")
    print(f"  target: {report['target']}")
    if not report["source_exists"]:
        print("  source does not exist — nothing to migrate")
        return
    if report["umbrella_gitignore_created"]:
        print("  .apiary/.gitignore created")
    print("  files:")
    for name, status in report["files"].items():
        print(f"    {name:<24} {status}")
    mem = report["memory"]
    nonzero = {k: v for k, v in mem.items() if v}
    if nonzero:
        parts = ", ".join(f"{v} {k}" for k, v in nonzero.items())
        print(f"  memory/: {parts}")
    else:
        print("  memory/: (empty or missing)")
    if any(s == "exists" for s in report["files"].values()) or mem.get("exists"):
        print("\n  Some target files already exist. Re-run with --force to overwrite.")
    elif report["dry_run"]:
        print("\n  Dry run complete. Re-run without --dry-run to actually copy.")
    else:
        print("\n  Migration complete. Legacy files left in place at the source.")
        print("  Verify the new setup with:")
        print('    APIARY_STATE_LAYOUT=repo python scribe/notes.py list')
        print("  Clean up the legacy location via todo #268 once you're satisfied.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        type=Path,
        default=LEGACY_DEFAULT,
        help=f"Legacy source directory (default: {LEGACY_DEFAULT})",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Target directory (default: <git-repo-root(cwd)>/.apiary/scribe/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be copied without touching the filesystem",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing target files",
    )
    args = parser.parse_args()

    target = args.target
    if target is None:
        target = _default_target()
        if target is None:
            print(
                "error: --target not specified and cwd is not inside a git repo",
                file=sys.stderr,
            )
            sys.exit(2)

    report = migrate(
        args.source.expanduser(),
        target.expanduser(),
        force=args.force,
        dry_run=args.dry_run,
    )
    _print_report(report)

    if not report["source_exists"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
