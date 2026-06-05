#!/usr/bin/env python3
"""Phase-3 migration: remove the apiary-managed zone from ``~/.claude/CLAUDE.md``.

Per MIGRATION-PLAN.md §10 phase 3 + §3.5 D20 + §13.7:

After per-repo bootstrap, every bootstrapped repo has its own
apiary-managed zone in ``<repo>/CLAUDE.md``. The global
``~/.claude/CLAUDE.md`` should retain only user-owned content. This
script deletes the apiary zone (sentinel-bounded block produced by
``core/context_rules.py``) and leaves everything outside the sentinels
intact.

Idempotent: re-running on a file with no zone exits cleanly.

If the zone has been hand-edited (tampered), the script refuses to
proceed unless ``--force`` is passed — same contract as
``scripts/install_context_rules.py``.

Usage::

    python scripts/phase3_strip_global_zone.py            # dry-run
    python scripts/phase3_strip_global_zone.py --apply
    python scripts/phase3_strip_global_zone.py --apply --force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import context_rules as cr


def _global_claude_md(override: Path | None = None) -> Path:
    if override is not None:
        return override
    return Path.home() / ".claude" / "CLAUDE.md"


def strip_zone(text: str, *, force: bool = False) -> tuple[str, bool, str]:
    """Return ``(new_text, removed, reason)``.

    ``removed`` is True if a zone was actually stripped. ``reason`` is a
    short human-readable note: "no zone", "stripped", or "tampered".
    Tampered zones are preserved unless ``force`` is True.
    """
    try:
        zone = cr.find_managed_zone(text)
    except cr.ZoneTamperError as exc:
        if not force:
            return text, False, f"tampered: {exc}"
        # force-strip: drop everything between the OUTER markers if we
        # can find them. Otherwise leave the file alone.
        start_idx = text.find(cr.OUTER_START)
        end_idx = text.find(cr.OUTER_END)
        if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
            return text, False, "tampered + can't locate sentinels"
        end_idx = end_idx + len(cr.OUTER_END)
        new_text = text[:start_idx] + text[end_idx:]
        return _tidy(new_text), True, "stripped (forced)"
    if zone is None:
        return text, False, "no zone"
    new_text = text[: zone.start] + text[zone.end :]
    return _tidy(new_text), True, "stripped"


def _tidy(text: str) -> str:
    """Collapse the trailing blank line that zone removal can leave."""
    if not text.strip():
        return ""
    return text.rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--target", type=Path, default=None,
        help="path to CLAUDE.md (default: ~/.claude/CLAUDE.md)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="strip even if the zone has been hand-edited (tampered)",
    )
    args = parser.parse_args(argv)

    target = _global_claude_md(args.target)
    if not target.is_file():
        print(f"no file at {target} — nothing to do.")
        return 0

    text = target.read_text(encoding="utf-8")
    new_text, removed, reason = strip_zone(text, force=args.force)

    if not removed:
        print(f"{target}: {reason}")
        return 0 if reason in ("no zone",) else 2

    print(f"{target}: would strip apiary zone ({len(text) - len(new_text)} chars removed)")
    if not args.apply:
        print("\ndry-run; pass --apply to write changes")
        return 0
    target.write_text(new_text, encoding="utf-8")
    print(f"applied: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
