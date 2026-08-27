#!/usr/bin/env python3
"""``apiary doctor`` — consistency checks for the per-repo install model.

Every check is read-only: running a check reports findings and mutates
nothing. Passing ``--fix`` alongside a check name opts into that check's
writer, where one exists — see ``FIXES`` below (``pointers``, ``pins``).

Subcommands:

- ``pointers``      — main-apiary's self-pointer matches its actual cwd
- ``pins``          — every registered repo's ``.claude/apiary/`` pins agree
                      with the registry (uid, name, main-apiary path), and
                      uid 1 really is main-apiary
- ``registry``      — every registry entry: path exists; uid/version present
- ``versions``      — each registered repo's version vs main-apiary's VERSION
- ``stale``         — registered repo whose installed slash-command files
                      differ from current main-apiary source (skill drift
                      that ``versions`` misses when the version is unchanged)
- ``orphans``       — folders under ``.repos/<slug>/`` with no registry entry
- ``duplicates``    — registry entries sharing a ``real_path``
- ``unreachable``   — registry entries whose ``real_path`` does not exist
- ``compass``       — compass measurement health: observation count, last
                      synthesis age, profile size, A/B arm counts and the
                      last ``compass/evaluate.py offline`` headline
                      (report-only — always notes, never issues)
- (no arg)          — run all checks, print a summary

Usage::

    poetry run apiary doctor
    poetry run apiary doctor registry
    poetry run apiary doctor pointers --fix

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
# a run; issues do. The split lets expected-but-worth-saying status (e.g. a
# machine where `apiary self-bootstrap` has not been run yet, so there is no
# self-pointer to compare) print without making the doctor exit nonzero.
CheckResult = tuple[list[str], list[str]]


def check_pointers(apiary: Path) -> CheckResult:
    """Verify main-apiary's own self-pointer matches its actual location.

    The pin files (``<apiary>/.claude/apiary/*``) are written by
    ``apiary self-bootstrap``. A checkout where that has never been run has
    no self-pointer to compare against, which is a setup step outstanding
    rather than drift — report it as a note, not an issue.
    """
    notes: list[str] = []
    issues: list[str] = []
    self_p = state.read_self_pointer(apiary)
    if self_p is None:
        notes.append(
            "main-apiary's self-pointer not yet written "
            f"(expected at {state.self_pointer_path(apiary)}); "
            "run `apiary self-bootstrap` to write it."
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


def check_versions(apiary: Path) -> CheckResult:
    """Compare each registered repo's pinned version against main-apiary's.

    Two records claim to know a repo's version: the registry entry and the
    repo's own ``.claude/apiary/version.json``. ``apiary update`` writes both
    in one step, so a disagreement means something else edited one of them —
    worth a note even when neither has drifted from main-apiary.
    """
    notes: list[str] = []
    issues: list[str] = []
    main_version = state.read_apiary_version(apiary)
    registry = state._load_registry(apiary)
    for id_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "?")
        real = entry.get("real_path", "")
        repo_version = entry.get("version")
        if repo_version is None:
            issues.append(f"registry[{id_str}] ({name}) has no `version` field")
            continue

        if real and Path(real).is_dir():
            pin = state.read_version(Path(real)) or {}
            pinned = pin.get("apiary_version")
            if pinned and pinned != repo_version:
                notes.append(
                    f"registry[{id_str}] ({name}) says {repo_version} but "
                    f"{Path(real).name}/.claude/apiary/version.json says {pinned}"
                )

        if repo_version != main_version:
            remediation = (
                f"run `apiary update --target \"{real}\"`"
                if real else "run `apiary update`"
            )
            issues.append(
                f"registry[{id_str}] ({name}) "
                f"pinned to {repo_version}; main-apiary is at {main_version} "
                f"— {remediation} to run the migration chain and re-pin."
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


# Uid 1 is main-apiary by convention, and `drift`, `cascade` and `install`
# all act on that assumption — drift's uid-1 branch rewrites OTHER repos'
# pointers. Nothing enforced it, so check_pins does.
MAIN_APIARY_UID = 1


def _same_dir(raw: str, resolved: Path) -> bool:
    """True when *raw* names the same directory as the already-resolved
    *resolved*. An empty or unresolvable value is never a match (``Path("")``
    resolves to the cwd, which would silently pass)."""
    if not raw:
        return False
    try:
        return Path(raw).resolve() == resolved
    except OSError:
        return False


def _pin_findings(apiary: Path, uid_str: str, entry: dict) -> CheckResult:
    """Compare one registered repo's pin files against its registry entry."""
    notes: list[str] = []
    issues: list[str] = []
    name = entry.get("name", "?")
    repo = Path(entry.get("real_path", ""))
    label = f"{name} (uid={uid_str})"

    self_p = state.read_self_pointer(repo)
    if self_p is None:
        notes.append(
            f"{label}: no self-pointer at {state.self_pointer_path(repo)} — "
            f"registered but not bootstrapped; run `apiary install --target \"{repo}\"`."
        )
    else:
        pinned_uid = self_p.get("uid")
        if str(pinned_uid) != uid_str:
            issues.append(
                f"{label}: self-pointer uid={pinned_uid} disagrees with the registry "
                f"({uid_str}). Its launcher looks for .repos/{self_p.get('name', name)}-"
                f"{pinned_uid}/, so every tool's state silently falls back. "
                "Run `apiary doctor pins --fix`."
            )
        elif self_p.get("name") != name:
            issues.append(
                f"{label}: self-pointer name={self_p.get('name')!r} disagrees with the "
                f"registry ({name!r}); its state dir resolves to the wrong slug. "
                "Run `apiary doctor pins --fix`."
            )

    main_p = state.read_main_apiary_pointer(repo)
    if main_p is None:
        if self_p is not None:
            issues.append(
                f"{label}: no main-apiary-pointer at "
                f"{state.main_apiary_pointer_path(repo)} — the repo cannot find "
                f"main-apiary. Run `apiary install --target \"{repo}\"`."
            )
    else:
        recorded = main_p.get("main_apiary_path", "")
        if not _same_dir(recorded, apiary):
            issues.append(
                f"{label}: main-apiary-pointer says {recorded or '<unset>'}, this "
                f"checkout is {apiary}. Run `apiary doctor pins --fix`."
            )
    return notes, issues


def check_pins(apiary: Path) -> CheckResult:
    """Every registered repo's pin files agree with its registry entry.

    The pin model's whole promise is that ``<repo>/.claude/apiary/`` and
    ``.repos/registry.json`` say the same thing. Nothing checked it, so the
    states that break it were invisible: a self-pointer uid left over from a
    lost registry entry (the launcher then derives a state dir nothing else
    knows about), a main-apiary-pointer aimed at an old checkout, or a uid 1
    that is not main-apiary at all.

    Unreachable repos are skipped — ``check_unreachable`` reports those, and
    there are no pins to read at a path that does not exist.
    """
    apiary = Path(apiary).resolve()
    notes: list[str] = []
    issues: list[str] = []
    registry = state._load_registry(apiary)

    for uid_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue  # check_registry reports the shape
        real = entry.get("real_path", "")
        if not real or not Path(real).is_dir():
            continue
        entry_notes, entry_issues = _pin_findings(apiary, uid_str, entry)
        notes.extend(entry_notes)
        issues.extend(entry_issues)

    uid_notes, uid_issues = _main_apiary_uid_findings(apiary, registry)
    return notes + uid_notes, issues + uid_issues


def _main_apiary_uid_findings(apiary: Path, registry: dict) -> CheckResult:
    """The uid-1-is-main-apiary invariant, checked in both directions."""
    notes: list[str] = []
    issues: list[str] = []
    match = state._find_entry_by_path(registry, apiary)
    if match is None:
        notes.append(
            f"main-apiary ({apiary}) is not in its own registry — "
            "run `apiary self-bootstrap`."
        )
    elif match[0] != str(MAIN_APIARY_UID):
        issues.append(
            f"main-apiary is registered as uid {match[0]}, but the drift handler, "
            f"cascade-fix and install all treat uid {MAIN_APIARY_UID} as main-apiary."
        )
    entry = registry.get(str(MAIN_APIARY_UID))
    if isinstance(entry, dict) and not _same_dir(entry.get("real_path", ""), apiary):
        issues.append(
            f"uid {MAIN_APIARY_UID} is {entry.get('name', '?')} "
            f"({entry.get('real_path', '') or '<unset>'}), not main-apiary ({apiary}). "
            "If that repo ever moves, the drift handler takes main-apiary's branch "
            "for it and rewrites other repos' pointers."
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


def check_compass(apiary: Path) -> CheckResult:
    """Report compass's measurement health for main-apiary's own state dir.

    Report-only on purpose: every finding is a note, never an issue. A stale
    profile or an A/B that has not been turned on is information the owner
    asked for (review §5a-H.3), not a broken install, and this check shares
    the doctor's exit code with checks that gate CI.
    """
    notes: list[str] = []
    try:
        from compass import health
        state_dir = state.find_state_dir(apiary)
        if state_dir is None:
            return ["compass: no state dir registered for main-apiary"], []
        notes = health.format_notes(health.collect(state_dir))
    except Exception as exc:  # never let a report-only check fail a doctor run
        notes = [f"compass: could not read health facts ({exc})"]
    return notes, []


CHECKS = {
    "pointers": check_pointers,
    "pins": check_pins,
    "registry": check_registry,
    "versions": check_versions,
    "stale": check_stale,
    "orphans": check_orphans,
    "duplicates": check_duplicates,
    "unreachable": check_unreachable,
    "compass": check_compass,
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


def _fix_pins(apiary: Path) -> int:
    """Rewrite each registered repo's pin files from the registry.

    The registry is the source of truth: it is what allocated the uid and what
    ``.repos/<name>-<uid>/`` is named after. Only the two fields that can
    disagree are rewritten — everything else in the pin (notably
    ``last_drift_check``) is carried over untouched.

    Repos with no pin files at all are left alone: the repair there is
    ``apiary install``, which writes the launcher and hooks too. Returns 1 if
    any issue survives the pass (e.g. a uid 1 that is not main-apiary, which
    needs a human to decide which repo keeps the uid).
    """
    apiary = Path(apiary).resolve()
    registry = state._load_registry(apiary)
    fixed = 0
    for uid_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        real = entry.get("real_path", "")
        if not real or not Path(real).is_dir():
            continue
        repo = Path(real)
        name = entry.get("name", "")
        self_p = state.read_self_pointer(repo)
        if self_p is not None and (str(self_p.get("uid")) != uid_str
                                   or self_p.get("name") != name):
            state.write_self_pointer(repo, {**self_p, "uid": int(uid_str), "name": name})
            print(f"  rewrote self-pointer for {name} (uid={uid_str}) at {repo}")
            fixed += 1
        main_p = state.read_main_apiary_pointer(repo)
        if main_p is not None and not _same_dir(main_p.get("main_apiary_path", ""), apiary):
            state.write_main_apiary_pointer(repo, {**main_p, "main_apiary_path": str(apiary)})
            print(f"  rewrote main-apiary-pointer for {name} (uid={uid_str}) at {repo}")
            fixed += 1

    print(f"[pins --fix] rewrote {fixed} pin file(s)")
    notes, issues = check_pins(apiary)
    for n in notes:
        print(f"  - note: {n}")
    for i in issues:
        print(f"  - issue (not auto-fixable): {i}")
    return 1 if issues else 0


FIXES = {
    "pointers": _fix_pointers,
    "pins": _fix_pins,
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
