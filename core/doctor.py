#!/usr/bin/env python3
"""``apiary doctor`` — read-only consistency checks for the per-repo install model.

Phase-0 scaffold. All subcommands report findings; none mutate state. The
``--fix`` flag is reserved for a later phase once the pointer/mailbox/cascade
write paths exist.

Subcommands (per MIGRATION-PLAN.md §7.6):

- ``pointers``      — main-apiary's self-pointer matches its actual cwd
- ``registry``      — every registry entry: path exists; uid/version present
- ``mailbox``       — count pending forwarding messages
- ``versions``      — each registered repo's version vs main-apiary's VERSION
- ``orphans``       — folders under ``.repos/<slug>/`` with no registry entry
- ``duplicates``    — registry entries sharing a ``real_path``
- ``unreachable``   — registry entries whose ``real_path`` does not exist
- (no arg)          — run all checks, print a summary

Usage::

    python ~/.claude/apiary_launch.py core/doctor.py
    python ~/.claude/apiary_launch.py core/doctor.py registry
    python ~/.claude/apiary_launch.py core/doctor.py mailbox

Exit code is 0 when all checks pass and 1 when any check reports a problem,
so the doctor can be wired into CI or a pre-commit hook later.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import state


# A check returns (notes, issues). Notes are informational and never fail
# a run; issues do. The split lets phase-0-expected status (e.g. self-pointer
# files not yet created) print without making the doctor exit nonzero.
CheckResult = tuple[list[str], list[str]]


def check_pointers(apiary: Path) -> CheckResult:
    """Verify main-apiary's own self-pointer matches its actual location.

    Phase-0 limitation: the per-repo pin files (``<apiary>/.claude/apiary/*``)
    are populated in phase 1 by ``apiary self-bootstrap``. If they don't
    exist yet, that's expected during the migration window — report as a
    note, not an issue.
    """
    notes: list[str] = []
    issues: list[str] = []
    self_p = state.read_self_pointer(apiary)
    if self_p is None:
        notes.append(
            "main-apiary's self-pointer not yet written "
            f"(expected at {state.self_pointer_path(apiary)}); "
            "run `apiary self-bootstrap` once phase 1 lands."
        )
        return notes, issues
    recorded = Path(self_p.get("real_path", ""))
    actual = apiary.resolve()
    if recorded.resolve() != actual:
        issues.append(
            f"main-apiary self-pointer drift: recorded={recorded}, actual={actual}. "
            "Run `apiary doctor pointers --fix` to cascade-fix all bootstrapped repos."
        )
    return notes, issues


def check_registry(apiary: Path) -> CheckResult:
    """Walk every registered repo and report missing fields or paths."""
    notes: list[str] = []
    issues: list[str] = []
    registry = state._load_registry(apiary)
    for id_str, entry in registry.items():
        if not isinstance(entry, dict):
            issues.append(f"registry[{id_str}] is not a dict")
            continue
        try:
            uid_int = int(id_str)
        except ValueError:
            issues.append(f"registry key {id_str!r} is not an integer")
            continue
        if "uid" not in entry:
            issues.append(f"registry[{id_str}] missing `uid` field")
        elif entry["uid"] != uid_int:
            issues.append(
                f"registry[{id_str}].uid={entry['uid']} disagrees with key ({uid_int})"
            )
        if "version" not in entry:
            issues.append(f"registry[{id_str}] missing `version` field")
        real_path = entry.get("real_path", "")
        if not real_path:
            issues.append(f"registry[{id_str}] missing `real_path`")
        elif not Path(real_path).is_dir():
            issues.append(
                f"registry[{id_str}] ({entry.get('name', '?')}) "
                f"real_path does not exist: {real_path}"
            )
    return notes, issues


def check_mailbox(apiary: Path) -> CheckResult:
    """Report pending forwarding messages. Processing is phase-1 work."""
    forwarding = apiary / ".apiary" / "forwarding"
    if not forwarding.is_dir():
        return [f"mailbox dir not yet created at {forwarding}"], []
    pending = [p for p in forwarding.glob("*.json") if p.is_file()]
    if not pending:
        return [], []
    return [], [
        f"{len(pending)} pending forwarding message(s) at {forwarding} "
        "— run `apiary doctor mailbox --fix` once phase 1 lands."
    ]


def check_versions(apiary: Path) -> CheckResult:
    """Compare each registered repo's pinned version against main-apiary's."""
    notes: list[str] = []
    issues: list[str] = []
    main_version = state.read_apiary_version(apiary)
    registry = state._load_registry(apiary)
    for id_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        repo_version = entry.get("version")
        if repo_version is None:
            issues.append(
                f"registry[{id_str}] ({entry.get('name', '?')}) has no `version` field"
            )
            continue
        if repo_version != main_version:
            issues.append(
                f"registry[{id_str}] ({entry.get('name', '?')}) "
                f"pinned to {repo_version}; main-apiary is at {main_version} "
                "— `apiary update` needed."
            )
    return notes, issues


def check_orphans(apiary: Path) -> CheckResult:
    """Folders under ``.repos/<slug>/`` whose UID has no registry entry."""
    notes: list[str] = []
    issues: list[str] = []
    repos_root = state.repos_dir(apiary)
    if not repos_root.is_dir():
        return notes, issues
    registry = state._load_registry(apiary)
    known_uids = {str(k) for k in registry.keys()}
    for child in repos_root.iterdir():
        if not child.is_dir():
            continue
        # Slug is "<name>-<uid>"; tolerate names containing dashes by
        # taking the trailing dash-segment.
        suffix = child.name.rsplit("-", 1)[-1] if "-" in child.name else ""
        if not suffix.isdigit():
            issues.append(f"unparseable slug in .repos/: {child.name}")
            continue
        if suffix not in known_uids:
            issues.append(f"orphan: .repos/{child.name} has no registry entry")
    return notes, issues


def check_duplicates(apiary: Path) -> CheckResult:
    """Two registry entries pointing at the same ``real_path``."""
    notes: list[str] = []
    issues: list[str] = []
    registry = state._load_registry(apiary)
    seen: dict[str, str] = {}
    for id_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        real = entry.get("real_path")
        if not real:
            continue
        if real in seen:
            issues.append(
                f"duplicate real_path: {real} appears in registry[{seen[real]}] "
                f"and registry[{id_str}]"
            )
        else:
            seen[real] = id_str
    return notes, issues


def check_unreachable(apiary: Path) -> CheckResult:
    """Registry entries whose ``real_path`` does not currently exist on disk."""
    notes: list[str] = []
    issues: list[str] = []
    registry = state._load_registry(apiary)
    for id_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        real = entry.get("real_path", "")
        if real and not Path(real).is_dir():
            issues.append(
                f"unreachable: registry[{id_str}] ({entry.get('name', '?')}) "
                f"real_path={real}"
            )
    return notes, issues


CHECKS = {
    "pointers": check_pointers,
    "registry": check_registry,
    "mailbox": check_mailbox,
    "versions": check_versions,
    "orphans": check_orphans,
    "duplicates": check_duplicates,
    "unreachable": check_unreachable,
}


def _run_one(name: str, apiary: Path) -> int:
    notes, issues = CHECKS[name](apiary)
    if not notes and not issues:
        print(f"[{name}] OK")
        return 0
    label = "OK" if not issues else f"{len(issues)} issue(s)"
    if notes:
        label = f"{label}, {len(notes)} note(s)"
    print(f"[{name}] {label}:")
    for n in notes:
        print(f"  - note: {n}")
    for i in issues:
        print(f"  - issue: {i}")
    return 0 if not issues else 1


def _run_all(apiary: Path) -> int:
    rc = 0
    for name in CHECKS:
        rc |= _run_one(name, apiary)
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apiary doctor",
        description="Read-only consistency checks for the per-repo install model.",
    )
    parser.add_argument(
        "subcommand", nargs="?", choices=list(CHECKS.keys()),
        help="check to run; omit to run all",
    )
    parser.add_argument(
        "--apiary-repo", type=Path, default=None,
        help="path to main-apiary checkout (default: resolved via launcher / pointer)",
    )
    args = parser.parse_args(argv)

    apiary = state.resolve_apiary_repo(args.apiary_repo)
    if args.subcommand:
        return _run_one(args.subcommand, apiary)
    return _run_all(apiary)


if __name__ == "__main__":
    raise SystemExit(main())
