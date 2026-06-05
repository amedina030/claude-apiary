#!/usr/bin/env python3
"""Phase-3 migration: split global ``~/.claude/.session-history.json``
into per-repo ``<main-apiary>/.repos/<slug>/sessions/history.json`` files.

Per MIGRATION-PLAN.md §10 phase 3 + §6.7 + §9.14:

- Today's session history is a single global list (one entry per Claude
  Code session across every repo on the machine).
- Post-migration, each registered repo has its own bounded history file
  at ``<main-apiary>/.repos/<slug>/sessions/history.json`` with the v1
  schema ``{schema_version, sessions: [...]}``.
- Entries are bucketed into the per-repo files by matching their
  ``transcript_path`` prefix to each registered repo's project key
  (cwd-derived by ``core.utils.project.project_key_from_path``).
- Entries that don't match any registered repo are archived to
  ``<main-apiary>/.apiary/legacy/orphan-session-history.json`` for one-
  time review (rather than silently dropped).

Idempotent: re-running merges new entries by ``session_id``, never
duplicates. ``--apply`` writes; default is dry-run.

Usage::

    python scripts/phase3_migrate_session_history.py
    python scripts/phase3_migrate_session_history.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import state
from core.utils.project import project_key_from_path

PER_REPO_HISTORY_SCHEMA = 1
ORPHAN_ARCHIVE_SUBPATH = ".apiary/legacy/orphan-session-history.json"
GLOBAL_HISTORY_FILENAME = ".session-history.json"


def _global_history_path(global_dir: Path | None = None) -> Path:
    g = global_dir if global_dir is not None else (Path.home() / ".claude")
    return g / GLOBAL_HISTORY_FILENAME


def _per_repo_history_path(apiary: Path, slug: str) -> Path:
    return state.repos_dir(apiary) / slug / "sessions" / "history.json"


def _orphan_archive_path(apiary: Path) -> Path:
    return Path(apiary) / ORPHAN_ARCHIVE_SUBPATH


def _load_history_list(p: Path) -> list[dict]:
    """Read the global history. Tolerates missing/malformed files."""
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _load_per_repo_history(p: Path) -> dict:
    """Load an existing per-repo history file, or return a fresh shell."""
    if not p.is_file():
        return {"schema_version": PER_REPO_HISTORY_SCHEMA, "sessions": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": PER_REPO_HISTORY_SCHEMA, "sessions": []}
    if not isinstance(data, dict) or "sessions" not in data:
        return {"schema_version": PER_REPO_HISTORY_SCHEMA, "sessions": []}
    return data


def _bucket_entries(
    entries: list[dict], registry: dict,
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Split *entries* into ``({slug: [entries]}, orphans)``.

    Each registered repo's project-key is derived from its ``real_path``;
    entries whose ``transcript_path`` contains that project-key as a path
    component are routed to that repo's bucket. Entries that don't match
    any registered repo go to ``orphans`` for archival.
    """
    # Build {project_key: slug} map. The project-key is the path-derived
    # one; consumers that use a stable .claude-project-key file already
    # use that for current sessions, but historical entries always
    # use the cwd-derived form (transcript_path was generated from cwd
    # at session-start time).
    key_to_slug: dict[str, str] = {}
    for uid_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        real_path = entry.get("real_path")
        name = entry.get("name")
        if not real_path or not name:
            continue
        key = project_key_from_path(real_path)
        slug = f"{name}-{uid_str}"
        key_to_slug[key] = slug

    buckets: dict[str, list[dict]] = {slug: [] for slug in key_to_slug.values()}
    orphans: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        transcript = entry.get("transcript_path", "") or ""
        # Match the project-key as a path segment. Use both forward- and
        # back-slash variants so Windows paths route correctly.
        matched_slug: str | None = None
        for key, slug in key_to_slug.items():
            if f"/{key}/" in transcript or f"\\{key}\\" in transcript:
                matched_slug = slug
                break
        if matched_slug is None:
            orphans.append(entry)
        else:
            buckets[matched_slug].append(entry)
    return buckets, orphans


def _merge_into_existing(existing: dict, incoming: list[dict]) -> tuple[dict, int]:
    """Append incoming entries to *existing*'s ``sessions`` list, deduping
    by ``session_id``. Returns (new_history, count_added)."""
    seen_ids = {
        s.get("session_id") for s in existing["sessions"] if isinstance(s, dict)
    }
    added = 0
    for entry in incoming:
        sid = entry.get("session_id")
        if sid and sid in seen_ids:
            continue
        existing["sessions"].append(entry)
        seen_ids.add(sid)
        added += 1
    return existing, added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--apiary-repo", type=Path, default=None)
    parser.add_argument(
        "--global-dir", type=Path, default=None,
        help="override ~/.claude (useful for tests)",
    )
    args = parser.parse_args(argv)

    apiary = state.resolve_apiary_repo(args.apiary_repo)
    history_path = _global_history_path(args.global_dir)
    entries = _load_history_list(history_path)
    if not entries:
        print(f"no entries in {history_path} — nothing to migrate.")
        return 0

    registry = state._load_registry(apiary)
    buckets, orphans = _bucket_entries(entries, registry)

    print(f"global history: {len(entries)} entries at {history_path}")
    for slug, items in buckets.items():
        print(f"  {slug}: {len(items)} entry(ies)")
    print(f"  orphans: {len(orphans)} entry(ies)")

    if not args.apply:
        print("\ndry-run; pass --apply to write changes")
        return 0

    written_files = 0
    added_total = 0
    for slug, items in buckets.items():
        if not items:
            continue
        dest = _per_repo_history_path(apiary, slug)
        existing = _load_per_repo_history(dest)
        existing, added = _merge_into_existing(existing, items)
        if added == 0:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        written_files += 1
        added_total += added
        print(f"  wrote {dest} (+{added} new)")

    if orphans:
        archive = _orphan_archive_path(apiary)
        existing_orphans = _load_history_list(archive)
        # Dedupe orphans by session_id too.
        seen = {
            o.get("session_id") for o in existing_orphans if isinstance(o, dict)
        }
        for entry in orphans:
            if entry.get("session_id") in seen:
                continue
            existing_orphans.append(entry)
            seen.add(entry.get("session_id"))
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(
            json.dumps(existing_orphans, indent=2) + "\n", encoding="utf-8",
        )
        print(f"  archived {len(orphans)} orphan(s) → {archive}")

    print(f"\napplied: {written_files} file(s) updated, {added_total} new entry(ies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
