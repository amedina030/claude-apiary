#!/usr/bin/env python3
"""Phase-3 migration: move transcripts and per-session identity files
into per-repo session dirs.

Per MIGRATION-PLAN.md §10 phase 3 + §6.7 (D16):

- ``~/.claude/transcripts/<sid>.jsonl`` → ``<main-apiary>/.repos/<slug>/sessions/transcripts/<sid>.jsonl``
- ``~/.claude/.session-identity-<short>.json`` → ``<main-apiary>/.repos/<slug>/sessions/identity-<short>.json``

The session-id → slug mapping is built from session history. This script
expects ``phase3_migrate_session_history.py`` to have already run (so the
per-repo history files exist) — it walks each per-repo history's
``transcript_path`` entries to build the slug map. Sessions whose
identity / transcript can't be routed are archived alongside the orphan
session-history entries for one-time review.

Idempotent: re-runs skip files already at their destination. Default is
dry-run; pass ``--apply`` to perform the moves.

Note: transcripts are *moved* (rename) when possible; identity files are
*copied* so a partially-rolled-out migration doesn't lose state. Both
behaviors are configurable via ``--copy-transcripts`` if you want to
preserve the global copy until you're sure.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import state
from core.utils.project import project_key_from_path

# Filename patterns
_IDENTITY_GLOB = ".session-identity-*.json"
_IDENTITY_RE = re.compile(r"^\.session-identity-(?P<short>.+)\.json$")
_TRANSCRIPT_GLOB = "*.jsonl"


def _global_dir(override: Path | None = None) -> Path:
    return override if override is not None else (Path.home() / ".claude")


def _orphan_archive_path(apiary: Path) -> Path:
    return Path(apiary) / ".apiary" / "legacy" / "orphan-session-state.json"


def _build_session_to_slug(apiary: Path, registry: dict) -> dict[str, str]:
    """Walk every per-repo ``sessions/history.json`` and return
    ``{full_session_id: slug}`` for every session known to apiary.

    Uses the per-repo history files (post-phase3_migrate_session_history)
    so the routing matches what session-history migration produced. Falls
    back to the global session-history.json if no per-repo file exists yet
    (lets this script run independently of session-history migration when
    the user wants).
    """
    sid_to_slug: dict[str, str] = {}

    for uid_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        slug = f"{name}-{uid_str}"
        per_repo = state.repos_dir(apiary) / slug / "sessions" / "history.json"
        if not per_repo.is_file():
            continue
        try:
            data = json.loads(per_repo.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for sess in data.get("sessions", []):
            if isinstance(sess, dict) and sess.get("session_id"):
                sid_to_slug[sess["session_id"]] = slug

    # Fallback: also scan global session-history.json directly (lets this
    # script run before session-history migration). The same routing logic
    # as in phase3_migrate_session_history is reused.
    g = Path.home() / ".claude" / ".session-history.json"
    if g.is_file():
        try:
            entries = json.loads(g.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            entries = []
        if isinstance(entries, list):
            key_to_slug: dict[str, str] = {}
            for uid_str, entry in registry.items():
                if not isinstance(entry, dict):
                    continue
                rp = entry.get("real_path")
                nm = entry.get("name")
                if not rp or not nm:
                    continue
                key_to_slug[project_key_from_path(rp)] = f"{nm}-{uid_str}"
            for e in entries:
                if not isinstance(e, dict):
                    continue
                sid = e.get("session_id")
                tp = e.get("transcript_path", "") or ""
                if not sid or sid in sid_to_slug:
                    continue
                for key, slug in key_to_slug.items():
                    if f"/{key}/" in tp or f"\\{key}\\" in tp:
                        sid_to_slug[sid] = slug
                        break
    return sid_to_slug


def plan_identity_moves(
    apiary: Path, sid_to_slug: dict[str, str], *, global_dir: Path | None = None,
) -> tuple[list[tuple[str, Path, Path]], list[Path]]:
    """Return ``(routed, orphans)`` for identity files.

    ``routed`` is ``[(short_or_full_sid, src, dest)]``. ``orphans`` is the
    list of identity files we couldn't route (no matching session_id).
    """
    g = _global_dir(global_dir)
    routed: list[tuple[str, Path, Path]] = []
    orphans: list[Path] = []
    # Session_id resolution: identity files use the short form. We match
    # against full ids from history by checking if any full_id starts with
    # the short id (the canonical short = first 8 hex chars).
    full_ids = list(sid_to_slug.keys())
    for f in sorted(g.glob(_IDENTITY_GLOB)):
        m = _IDENTITY_RE.match(f.name)
        if not m:
            continue
        short = m.group("short")
        match = next((sid for sid in full_ids if sid.startswith(short)), None)
        if match is None:
            orphans.append(f)
            continue
        slug = sid_to_slug[match]
        dest = state.repos_dir(apiary) / slug / "sessions" / f"identity-{short}.json"
        if dest.is_file():
            continue  # already migrated
        routed.append((short, f, dest))
    return routed, orphans


def plan_transcript_moves(
    apiary: Path, sid_to_slug: dict[str, str], *, global_dir: Path | None = None,
) -> tuple[list[tuple[str, Path, Path]], list[Path]]:
    """Return ``(routed, orphans)`` for transcript files at
    ``~/.claude/transcripts/<sid>.jsonl``."""
    g = _global_dir(global_dir)
    src_dir = g / "transcripts"
    if not src_dir.is_dir():
        return [], []
    routed: list[tuple[str, Path, Path]] = []
    orphans: list[Path] = []
    for f in sorted(src_dir.glob(_TRANSCRIPT_GLOB)):
        sid_stem = f.stem
        # Match exactly against full session_ids first, then prefix.
        match = sid_stem if sid_stem in sid_to_slug else None
        if match is None:
            match = next((sid for sid in sid_to_slug if sid.startswith(sid_stem)), None)
        if match is None:
            orphans.append(f)
            continue
        slug = sid_to_slug[match]
        dest = state.repos_dir(apiary) / slug / "sessions" / "transcripts" / f.name
        if dest.is_file():
            continue
        routed.append((sid_stem, f, dest))
    return routed, orphans


def _archive_orphans(apiary: Path, orphans: list[Path], *, kind: str) -> None:
    """Append a record (one entry per orphan file) to the orphan archive
    so the user can later inspect what couldn't be routed."""
    if not orphans:
        return
    archive = _orphan_archive_path(apiary)
    existing: list[dict] = []
    if archive.is_file():
        try:
            data = json.loads(archive.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
        except (OSError, json.JSONDecodeError):
            pass
    seen = {(e.get("kind"), e.get("path")) for e in existing if isinstance(e, dict)}
    for f in orphans:
        key = (kind, str(f))
        if key in seen:
            continue
        existing.append({"kind": kind, "path": str(f)})
        seen.add(key)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apiary-repo", type=Path, default=None)
    parser.add_argument("--global-dir", type=Path, default=None)
    parser.add_argument(
        "--copy-transcripts", action="store_true",
        help="copy transcripts instead of moving (preserves global file)",
    )
    args = parser.parse_args(argv)

    apiary = state.resolve_apiary_repo(args.apiary_repo).resolve()
    registry = state._load_registry(apiary)
    sid_to_slug = _build_session_to_slug(apiary, registry)
    print(f"resolved {len(sid_to_slug)} session(s) to slugs")

    routed_id, orphan_id = plan_identity_moves(apiary, sid_to_slug, global_dir=args.global_dir)
    routed_tx, orphan_tx = plan_transcript_moves(apiary, sid_to_slug, global_dir=args.global_dir)

    print(f"\nidentity files: {len(routed_id)} to migrate, {len(orphan_id)} orphan(s)")
    print(f"transcripts:    {len(routed_tx)} to migrate, {len(orphan_tx)} orphan(s)")

    if not args.apply:
        if routed_id or routed_tx:
            print("\ndry-run; pass --apply to perform moves")
        return 0

    moved = 0
    for _short, src, dest in routed_id:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)  # identity copies stay safe
        moved += 1
    for _sid, src, dest in routed_tx:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if args.copy_transcripts:
            shutil.copy2(src, dest)
        else:
            shutil.move(str(src), str(dest))
        moved += 1

    _archive_orphans(apiary, orphan_id, kind="identity")
    _archive_orphans(apiary, orphan_tx, kind="transcript")

    print(f"\napplied: moved/copied {moved} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
