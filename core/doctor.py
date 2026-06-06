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
- ``stale``         — registered repo whose installed slash-command files
                      differ from current main-apiary source (skill drift
                      that ``versions`` misses when the version is unchanged)
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
import json
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
            real = entry.get("real_path", "")
            remediation = (
                f"run `apiary install --target \"{real}\"` to update"
                if real
                else "re-run `apiary install --target <path>` to update"
            )
            issues.append(
                f"registry[{id_str}] ({entry.get('name', '?')}) "
                f"pinned to {repo_version}; main-apiary is at {main_version} "
                f"— {remediation}."
            )
    return notes, issues


def check_stale(apiary: Path) -> CheckResult:
    """Flag repos whose installed slash-command files differ from current source.

    Compares each registered repo's recorded ``commands_dir_hashes`` (written
    into ``bootstrap_state.json`` at install time) against the SHA-256 of the
    current ``<tool>/commands/*.md`` source in main-apiary. A mismatch means a
    re-install would change the repo's skills — i.e. the repo is running stale
    slash commands. This catches doc/skill edits that ``check_versions`` misses
    because they don't bump the pinned version.

    Reuses install's own hashing helpers so the expected set is byte-identical
    to what an install would write (no spurious drift from a divergent hash).
    """
    from core.install import _hash_file, _slash_command_sources

    notes: list[str] = []
    issues: list[str] = []

    # Expected: hash every current source command file, keyed by filename —
    # this is exactly the {name: sha256} map a fresh install would record.
    expected = {src.name: _hash_file(src) for src in _slash_command_sources(apiary)}

    registry = state._load_registry(apiary)
    for id_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "?")
        real = entry.get("real_path", "")
        # Unreachable repos are reported by check_unreachable; skip here so we
        # don't double-flag and so we never hash against a missing tree.
        if not real or not Path(real).is_dir():
            continue

        bs_path = state.repos_dir(apiary) / f"{name}-{id_str}" / "bootstrap_state.json"
        if not bs_path.is_file():
            notes.append(
                f"{name} (uid={id_str}): no bootstrap_state.json — pre-v2 install; "
                f"run `apiary install --target \"{real}\"` to enable drift detection."
            )
            continue
        try:
            recorded = json.loads(bs_path.read_text(encoding="utf-8")).get("commands_dir_hashes")
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"{name} (uid={id_str}): unreadable bootstrap_state.json ({exc})")
            continue
        if not isinstance(recorded, dict):
            notes.append(
                f"{name} (uid={id_str}): bootstrap_state has no commands_dir_hashes — "
                f"run `apiary install --target \"{real}\"` to enable drift detection."
            )
            continue

        changed = sorted(fn for fn, h in expected.items() if recorded.get(fn) != h)
        removed = sorted(fn for fn in recorded if fn not in expected)
        drift = changed + [f"{fn} (removed)" for fn in removed]
        if drift:
            shown = ", ".join(drift[:5])
            more = f" (+{len(drift) - 5} more)" if len(drift) > 5 else ""
            issues.append(
                f"{name} (uid={id_str}) has {len(drift)} stale slash-command file(s): "
                f"{shown}{more} — run `apiary install --target \"{real}\"` to update."
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
    "stale": check_stale,
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


# --- --fix actions -------------------------------------------------------
# Each entry maps a subcommand name to a writer that performs the safe
# fix. Subcommands not in this dict don't have a fix yet — callers that
# pass --fix to one of them get a clear "not implemented" message.

def _fix_mailbox(apiary: Path) -> int:
    """Drain the mailbox: apply pending update_path / register_copy messages."""
    from core import mailbox
    report = mailbox.process_pending(apiary)
    print(f"[mailbox --fix] processed {report['processed']} message(s)")
    for entry in report["applied"]:
        print(f"  applied: uid={entry['uid']} kind={entry['kind']} "
              f"new_path={entry['new_path']}")
    for err in report["errors"]:
        print(f"  error: {err['file']} -> {err['reason']}")
    return 0 if not report["errors"] else 1


def _fix_pointers(apiary: Path) -> int:
    """Cascade-fix all bootstrapped repos' main-apiary-pointer to the
    current main-apiary location. Idempotent."""
    from core import cascade
    report = cascade.cascade_fix(apiary)
    print(f"[pointers --fix] cascade-fix at {report.new_main_apiary_path}")
    print(f"  updated {len(report.updated)} repo(s); skipped {len(report.skipped)}")
    for uid in report.updated:
        print(f"  updated uid={uid}")
    for uid, reason in report.skipped:
        print(f"  skipped uid={uid}: {reason}")
    return 0


FIXES = {
    "mailbox": _fix_mailbox,
    "pointers": _fix_pointers,
}


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
        "--fix", action="store_true",
        help=("apply safe fixes for the named subcommand. Currently supported: "
              f"{', '.join(FIXES.keys())}. Other subcommands report only."),
    )
    parser.add_argument(
        "--apiary-repo", type=Path, default=None,
        help="path to main-apiary checkout (default: resolved via launcher / pointer)",
    )
    args = parser.parse_args(argv)

    apiary = state.resolve_apiary_repo(args.apiary_repo)
    if args.fix:
        if not args.subcommand:
            print("--fix requires a subcommand; pick one of: "
                  f"{', '.join(FIXES.keys())}", file=sys.stderr)
            return 2
        if args.subcommand not in FIXES:
            print(f"--fix is not implemented for `{args.subcommand}` "
                  f"(supported: {', '.join(FIXES.keys())})", file=sys.stderr)
            return 2
        return FIXES[args.subcommand](apiary)

    if args.subcommand:
        return _run_one(args.subcommand, apiary)
    return _run_all(apiary)


if __name__ == "__main__":
    raise SystemExit(main())
