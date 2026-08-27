#!/usr/bin/env python3
"""``apiary update`` — bring every bootstrapped repo up to main-apiary's version.

The pieces have existed since phase 0 and were wired to nothing: ``VERSION``
holds main-apiary's pin, every bootstrapped repo carries
``.claude/apiary/version.json``, ``migrations/v<from>_to_v<to>.py`` documents
a contract, and ``apiary doctor versions`` reports the gap. Nothing closed it
— the remediation told the reader to re-run ``apiary install``, which rewrites
files but has no idea a migration exists (review §5a-I, decision 7).

What this does, per registered repo:

1. Read its pinned version (``version.json``, falling back to the registry
   entry) and compare with ``<main-apiary>/VERSION``.
2. Walk the migration chain from that version towards main-apiary's, applying
   each ``upgrade(repo_path)`` in order.
3. Rewrite the pin — after **each** step, not once at the end, so an
   interrupted run resumes from where it stopped rather than replaying
   migrations that already ran.

Not every version bump needs a migration. A gap with no matching module is
not an error: the chain stops, the pin is set to main-apiary's version, and
the repo is reported as updated with an empty migration list.

Failure semantics follow ``migrations/README.md``: ``upgrade()`` raising
leaves the pin at the last version that completed, aborts that repo's chain,
and does not stop the other repos. The command exits 1 if any repo failed.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import state
from core.utils.filelock import FileLock
from core.utils.timeutil import now_iso

MIGRATIONS_DIRNAME = "migrations"
MIGRATION_RE = re.compile(r"^v(\d+_\d+_\d+)_to_v(\d+_\d+_\d+)\.py$")

# Outcomes, in the order a summary line reads best.
CURRENT = "current"
UPDATED = "updated"
SKIPPED = "skipped"
FAILED = "failed"


class UpdateError(Exception):
    """A migration module is unusable (bad attributes, import failure)."""


@dataclass
class Migration:
    """One ``migrations/v<from>_to_v<to>.py`` module, already imported."""
    path: Path
    from_version: str
    to_version: str
    module: ModuleType

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class RepoResult:
    uid: int
    name: str
    path: Path
    from_version: str
    to_version: str
    status: str
    applied: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class UpdateReport:
    apiary_version: str
    results: list[RepoResult] = field(default_factory=list)

    @property
    def failed(self) -> list[RepoResult]:
        return [r for r in self.results if r.status == FAILED]

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)


# --- version helpers ------------------------------------------------------

def parse_version(text: str) -> tuple[int, int, int] | None:
    """``"0.2.1"`` → ``(0, 2, 1)``; None for anything not 3-part numeric semver.

    Deliberately strict: a version we cannot order is a version we must not
    silently migrate past.
    """
    parts = str(text).strip().split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return None
    return major, minor, patch


def _version_from_filename_part(part: str) -> str:
    return part.replace("_", ".")


# --- migration discovery --------------------------------------------------

def load_migrations(apiary: Path) -> list[Migration]:
    """Import every migration module under ``<apiary>/migrations/``.

    Sorted by target version so a chain walk is deterministic. Raises
    :class:`UpdateError` for a file that matches the naming convention but
    cannot be imported or disagrees with its own filename — a broken
    migration must be loud, never skipped.
    """
    migrations_dir = Path(apiary) / MIGRATIONS_DIRNAME
    found: list[Migration] = []
    if not migrations_dir.is_dir():
        return found

    for path in sorted(migrations_dir.glob("v*_to_v*.py")):
        match = MIGRATION_RE.match(path.name)
        if match is None:
            continue
        module = _import_migration(path)
        from_v = getattr(module, "FROM_VERSION", None)
        to_v = getattr(module, "TO_VERSION", None)
        if not from_v or not to_v:
            raise UpdateError(f"{path.name}: missing FROM_VERSION/TO_VERSION")
        if not callable(getattr(module, "upgrade", None)):
            raise UpdateError(f"{path.name}: no callable upgrade(repo_path)")
        expected_from = _version_from_filename_part(match.group(1))
        expected_to = _version_from_filename_part(match.group(2))
        if (from_v, to_v) != (expected_from, expected_to):
            raise UpdateError(
                f"{path.name}: declares {from_v} -> {to_v} but the filename "
                f"says {expected_from} -> {expected_to}"
            )
        found.append(Migration(path, from_v, to_v, module))

    found.sort(key=lambda m: (parse_version(m.to_version) or (0, 0, 0)))
    return found


def _import_migration(path: Path) -> ModuleType:
    """Import *path* under a private module name (never on sys.path)."""
    spec = importlib.util.spec_from_file_location(f"_apiary_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise UpdateError(f"{path.name}: cannot be imported")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — any import error is fatal here
        raise UpdateError(f"{path.name}: import failed ({exc})") from exc
    return module


def plan_chain(
    migrations: list[Migration], from_version: str, to_version: str,
) -> list[Migration]:
    """The migrations to run to get *from_version* to *to_version*.

    Walks FROM → TO links. Stops when no migration continues the chain (a
    version bump that needed no migration) or when the next hop would
    overshoot the target. Cycles are impossible to walk into because every
    hop must strictly increase the version.
    """
    target = parse_version(to_version)
    current = parse_version(from_version)
    if target is None or current is None:
        return []

    by_from: dict[str, Migration] = {}
    for migration in migrations:
        by_from.setdefault(migration.from_version, migration)

    chain: list[Migration] = []
    seen: set[str] = set()
    cursor = from_version
    while cursor != to_version:
        migration = by_from.get(cursor)
        if migration is None or migration.to_version in seen:
            break
        next_version = parse_version(migration.to_version)
        if next_version is None or next_version > target:
            break
        chain.append(migration)
        seen.add(migration.to_version)
        cursor = migration.to_version
    return chain


# --- the update itself ----------------------------------------------------

def repo_version(repo: Path, registry_entry: dict | None = None) -> str | None:
    """The version *repo* is pinned to, or None when it has no pin.

    ``version.json`` is the repo's own record and wins; the registry entry is
    the fallback for a repo whose pin file was lost (a wiped ``.claude/``).
    """
    pin = state.read_version(repo)
    if pin and pin.get("apiary_version"):
        return str(pin["apiary_version"])
    if registry_entry and registry_entry.get("version"):
        return str(registry_entry["version"])
    return None


def _write_pin(apiary: Path, repo: Path, uid: int, version: str) -> None:
    """Record *version* in both places that claim to know it.

    The registry half is a read-modify-write, so it takes the same FileLock
    ``install`` and the drift handler take — a session bootstrapping a repo
    while an update is running must not lose its entry.
    """
    existing = state.read_version(repo) or {}
    state.write_version(repo, {**existing, "apiary_version": version, "pinned_at": now_iso()})
    with FileLock(state.registry_path(apiary)):
        registry = state._load_registry(apiary)
        entry = registry.get(str(uid))
        if isinstance(entry, dict):
            entry["version"] = version
            registry[str(uid)] = entry
            state._save_registry(apiary, registry)


def update_repo(
    apiary: Path,
    uid: int,
    entry: dict,
    migrations: list[Migration],
    apiary_version: str,
    *,
    dry_run: bool = False,
) -> RepoResult:
    """Migrate one registered repo. Never raises for a per-repo problem."""
    name = str(entry.get("name", "?"))
    real = str(entry.get("real_path", ""))
    repo = Path(real)

    def result(status: str, from_v: str, to_v: str, **kw) -> RepoResult:
        return RepoResult(uid=uid, name=name, path=repo, from_version=from_v,
                          to_version=to_v, status=status, **kw)

    if not real or not repo.is_dir():
        return result(SKIPPED, "?", apiary_version,
                      detail=f"real_path does not exist: {real or '(empty)'}")

    current = repo_version(repo, entry)
    if current is None:
        return result(SKIPPED, "?", apiary_version,
                      detail="no version pin — run `apiary install --target` first")

    parsed_current, parsed_target = parse_version(current), parse_version(apiary_version)
    if parsed_current is None:
        return result(SKIPPED, current, apiary_version,
                      detail=f"pinned version {current!r} is not 3-part semver")
    if parsed_target is None:
        return result(SKIPPED, current, apiary_version,
                      detail=f"main-apiary VERSION {apiary_version!r} is not 3-part semver")
    if parsed_current == parsed_target:
        return result(CURRENT, current, apiary_version)
    if parsed_current > parsed_target:
        return result(SKIPPED, current, apiary_version,
                      detail="repo is pinned ahead of main-apiary; update main-apiary first")

    chain = plan_chain(migrations, current, apiary_version)
    if dry_run:
        return result(UPDATED, current, apiary_version,
                      applied=[m.name for m in chain], detail="dry run")

    reached = current
    for migration in chain:
        try:
            migration.module.upgrade(repo)
        except Exception as exc:  # noqa: BLE001 — a migration may raise anything
            return result(
                FAILED, current, apiary_version,
                applied=[m.name for m in chain[:chain.index(migration)]],
                detail=f"{migration.name} failed: {exc}",
            )
        reached = migration.to_version
        _write_pin(apiary, repo, uid, reached)

    if reached != apiary_version:
        # The remaining hops need no migration; the pin still has to move.
        _write_pin(apiary, repo, uid, apiary_version)
    return result(UPDATED, current, apiary_version, applied=[m.name for m in chain])


def update(
    apiary_repo: Path | None = None,
    *,
    target: Path | None = None,
    dry_run: bool = False,
) -> UpdateReport:
    """Update every registered repo, or just *target*."""
    apiary = state.resolve_apiary_repo(apiary_repo).resolve()
    apiary_version = state.read_apiary_version(apiary)
    migrations = load_migrations(apiary)
    report = UpdateReport(apiary_version=apiary_version)

    registry = state._load_registry(apiary)
    wanted = Path(target).resolve() if target is not None else None
    for uid_str, entry in sorted(registry.items(), key=lambda kv: _uid_key(kv[0])):
        if not isinstance(entry, dict):
            continue
        if wanted is not None:
            real = entry.get("real_path", "")
            if not real or Path(real).resolve() != wanted:
                continue
        report.results.append(
            update_repo(apiary, _uid_key(uid_str), entry, migrations,
                        apiary_version, dry_run=dry_run)
        )

    if wanted is not None and not report.results:
        raise UpdateError(
            f"{wanted} is not a registered repo. Run "
            f"`apiary install --target \"{wanted}\"` first."
        )
    return report


def _uid_key(uid_str: str) -> int:
    try:
        return int(uid_str)
    except (TypeError, ValueError):
        return 0


# --- CLI ------------------------------------------------------------------

def render(report: UpdateReport, *, dry_run: bool = False) -> str:
    """Human-readable summary. One line per repo, then a tally."""
    lines = [f"main-apiary version: {report.apiary_version}"
             + ("  (dry run — nothing written)" if dry_run else "")]
    for r in report.results:
        if r.status == CURRENT:
            lines.append(f"  [current] {r.name} (uid={r.uid}) at {r.from_version}")
            continue
        if r.status == SKIPPED:
            lines.append(f"  [skipped] {r.name} (uid={r.uid}): {r.detail}")
            continue
        applied = ", ".join(r.applied) if r.applied else "no migration needed"
        if r.status == FAILED:
            lines.append(f"  [FAILED]  {r.name} (uid={r.uid}) "
                         f"{r.from_version} -> {r.to_version}: {r.detail}")
            continue
        lines.append(f"  [updated] {r.name} (uid={r.uid}) "
                     f"{r.from_version} -> {r.to_version} ({applied})")
    lines.append(
        f"{report.count(UPDATED)} updated, {report.count(CURRENT)} already current, "
        f"{report.count(SKIPPED)} skipped, {report.count(FAILED)} failed"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apiary update",
        description="Run pending migrations/ and re-pin bootstrapped repos.",
    )
    parser.add_argument(
        "--target", type=Path, default=None,
        help="update only this repo (default: every registered repo)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the migrations that would run; write nothing",
    )
    parser.add_argument(
        "--apiary-repo", type=Path, default=None,
        help="path to main-apiary checkout (default: resolved via launcher / pointer)",
    )
    args = parser.parse_args(argv)

    try:
        report = update(args.apiary_repo, target=args.target, dry_run=args.dry_run)
    except UpdateError as exc:
        print(f"apiary update: {exc}", file=sys.stderr)
        return 1
    print(render(report, dry_run=args.dry_run))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
