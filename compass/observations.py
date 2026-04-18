#!/usr/bin/env python3
"""Compass observation CLI — list, count, validate, archive.

Per-session observation files are written by ``/wrapup`` (inline) and by
``compass/backfill.py`` (for historical sessions). This CLI is the
inspection and maintenance surface.

Usage:
    observations.py list [--full] [--archive]
    observations.py count
    observations.py validate <path>
    observations.py archive [--apply]   # dry-run by default

Archive policy (per spec C-2026-30):
  - Never archive when active count is below ARCHIVE_MIN_ACTIVE (50).
  - Files older than ARCHIVE_MAX_AGE_DAYS (90) are candidates.
  - Candidates move to ``observations/archive/<iso-year>-<iso-week>/``.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compass import store


def cmd_count(_args: argparse.Namespace) -> int:
    print(store.count_active_observations())
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if args.archive:
        root = store.archive_dir()
        files: list[Path] = sorted(root.rglob("*.json")) if root.exists() else []
    else:
        files = store.list_active_observations()

    if not files:
        print("(no observation files)", file=sys.stderr)
        return 0

    for path in files:
        if args.full:
            print(f"=== {path.name} ===")
            print(path.read_text(encoding="utf-8"))
        else:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                sid = data.get("session_id", "?")
                captured = data.get("captured_at", "?")
                count = len(data.get("observations", []))
                print(f"{path.name}\tsession={sid}\tcaptured={captured}\tn={count}")
            except (OSError, json.JSONDecodeError) as exc:
                print(f"{path.name}\t<unreadable: {exc}>")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unreadable JSON: {exc}", file=sys.stderr)
        return 1
    expected = path.stem if not args.no_filename_check else None
    errors = store.validate_observation(data, expected_session_id=expected)
    if errors:
        for e in errors:
            print(f"- {e}", file=sys.stderr)
        return 1
    print("ok")
    return 0


def _archive_candidates(now: datetime) -> list[Path]:
    cutoff = now - timedelta(days=store.ARCHIVE_MAX_AGE_DAYS)
    cutoff_ts = cutoff.timestamp()
    out: list[Path] = []
    for path in store.list_active_observations():
        if path.stat().st_mtime < cutoff_ts:
            out.append(path)
    return out


def cmd_archive(args: argparse.Namespace) -> int:
    active = store.list_active_observations()
    active_count = len(active)
    if active_count < store.ARCHIVE_MIN_ACTIVE:
        print(
            f"active count {active_count} < {store.ARCHIVE_MIN_ACTIVE}; "
            "skipping archive (preserves data while building signal)"
        )
        return 0

    now = datetime.now(timezone.utc)
    candidates = _archive_candidates(now)
    if not candidates:
        print(f"no files older than {store.ARCHIVE_MAX_AGE_DAYS} days")
        return 0

    if not args.apply:
        print(f"dry-run: would archive {len(candidates)} file(s):")
        for p in candidates:
            print(f"  {p.name}")
        print("re-run with --apply to move them.")
        return 0

    moved = 0
    for src in candidates:
        dest = store.archive_target_path(src, now=now)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        moved += 1
    print(f"archived {moved} file(s) to {store.archive_dir()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Compass observation CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_count = sub.add_parser("count", help="print active observation count")
    p_count.set_defaults(func=cmd_count)

    p_list = sub.add_parser("list", help="list observation files")
    p_list.add_argument("--full", action="store_true", help="print full JSON")
    p_list.add_argument("--archive", action="store_true", help="list archived files instead")
    p_list.set_defaults(func=cmd_list)

    p_validate = sub.add_parser("validate", help="validate one observation file")
    p_validate.add_argument("path")
    p_validate.add_argument("--no-filename-check", action="store_true",
                            help="skip session_id vs filename match check")
    p_validate.set_defaults(func=cmd_validate)

    p_archive = sub.add_parser("archive", help="archive sweep (dry-run by default)")
    p_archive.add_argument("--apply", action="store_true", help="actually move files")
    p_archive.set_defaults(func=cmd_archive)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
