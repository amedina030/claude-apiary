#!/usr/bin/env python3
"""Read-only importer: migrate a legacy per-repo ``.scribe/`` store into apiary.

Parity spec §7. Reads the legacy store **read-only**, recreates each note via
apiary's public API (apiary allocates fresh seq/year/timestamp), back-stamps
provenance (``orig_display_id`` + ``orig_timestamp``), carries over
``tags``/``areas``/``status``/``session`` and — for learnings — ``supersedes``,
rewrites ``supersedes`` to the new id via an old→new map, and **skips** ``ticket``
notes (apiary holds todo-mirrors only, spec F2). Supports ``--dry-run`` into a
scratch state-dir with an equivalence report.

Per the cross-machine agreement: apiary ships this code plus a synthetic test
fixture only. The real migration against real legacy data runs on the source
machine after it pulls the prepared apiary — real data never crosses machines.

Usage:
    import_legacy <legacy-.scribe-dir> <target-name-or-repo-path> [--dry-run] [--apiary-repo PATH]
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scribe import api
from scribe.store import ARCHIVE_DIRNAME, INDEX_FILENAME, ScribeStore, _parse_learning_content

# Legacy plural folder -> apiary note type. `tickets` is intentionally absent
# (skipped, F2). Apiary supports every type listed here.
LEGACY_TYPE_FOLDERS = {
    'todos': 'todo', 'handoffs': 'handoff', 'decisions': 'decision',
    'wishlists': 'wishlist', 'references': 'reference', 'blockers': 'blocker',
    'context': 'context', 'general': 'general', 'learnings': 'learning',
}
SKIP_FOLDERS = ('tickets',)


def _read_index_lines(path: Path) -> list:
    """Read a legacy index.jsonl, skipping blank and corrupt lines (tolerant)."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_legacy_notes(legacy_dir) -> list:
    """Return a list of ``{type, entry, body, archived}`` for every importable
    legacy note (all non-ticket types, active + archive). Read-only."""
    legacy_dir = Path(legacy_dir)
    notes = []
    for folder, ntype in LEGACY_TYPE_FOLDERS.items():
        fdir = legacy_dir / folder
        if not fdir.is_dir():
            continue
        for year_dir in sorted(p for p in fdir.iterdir() if p.is_dir() and p.name.isdigit()):
            for archived, idir in ((False, year_dir), (True, year_dir / ARCHIVE_DIRNAME)):
                for entry in _read_index_lines(idir / INDEX_FILENAME):
                    seq = entry.get('seq')
                    body = ''
                    if isinstance(seq, int):
                        bpath = idir / f"{seq}.md"
                        if bpath.is_file():
                            body = bpath.read_text(encoding='utf-8')
                    notes.append({'type': ntype, 'entry': entry, 'body': body, 'archived': archived})
    return notes


def count_skipped(legacy_dir) -> int:
    """Count legacy ticket notes (active + archive) that the importer skips."""
    legacy_dir = Path(legacy_dir)
    n = 0
    for folder in SKIP_FOLDERS:
        fdir = legacy_dir / folder
        if not fdir.is_dir():
            continue
        for year_dir in (p for p in fdir.iterdir() if p.is_dir() and p.name.isdigit()):
            for idir in (year_dir, year_dir / ARCHIVE_DIRNAME):
                n += len(_read_index_lines(idir / INDEX_FILENAME))
    return n


def import_into(store: ScribeStore, legacy_notes: list) -> tuple:
    """Import *legacy_notes* into *store*. Returns ``(id_map, counts)``.

    Notes are processed in original ``(year, seq)`` order so a learning's
    ``supersedes`` reference resolves to the already-imported target's new id.
    """
    id_map = {}
    counts = {}
    for n in sorted(legacy_notes, key=lambda x: (x['entry'].get('year', 0), x['entry'].get('seq', 0))):
        e, ntype, body = n['entry'], n['type'], n['body']
        old_id = e.get('display_id')
        meta = {'orig_display_id': old_id, 'orig_timestamp': e.get('timestamp')}
        for k in ('role', 'mission'):
            if e.get(k):
                meta[k] = e[k]
        if e.get('auto_generated'):
            meta['auto_generated'] = True
        session = e.get('session') or ''

        if ntype == 'learning':
            fm, real_body = _parse_learning_content(body)
            tags = e.get('tags') or fm.get('tags') or []
            areas = e.get('areas') or fm.get('areas') or []
            old_sup = e.get('supersedes') or fm.get('supersedes')
            new_sup = id_map.get(old_sup, old_sup) if old_sup else None
            new = store.add_learning(content=real_body, session_id=session,
                                     summary=e.get('summary', '') or '',
                                     brief_summary=e.get('brief_summary', '') or '',
                                     tags=tags, areas=areas, supersedes=new_sup, **meta)
        else:
            if e.get('tags'):
                meta['tags'] = e['tags']
            if e.get('areas'):
                meta['areas'] = e['areas']
            new = store.add_note(ntype, body, session_id=session,
                                 summary=e.get('summary', '') or '',
                                 brief_summary=e.get('brief_summary', '') or '', **meta)
            status = e.get('status', 'active')
            if status and status != 'active':
                store.update_note(ntype, new['year'], new['seq'], status=status)

        id_map[old_id] = new['display_id']
        if n['archived']:
            if ntype == 'learning':
                store.archive_learning(new['year'], new['seq'])
            else:
                store.archive_note(ntype, new['year'], new['seq'])
        counts[ntype] = counts.get(ntype, 0) + 1
    return id_map, counts


def equivalence_report(legacy_notes: list, counts: dict, skipped: int, id_map: dict) -> str:
    """Per-type in (legacy) vs out (apiary) counts, plus the skipped-ticket line."""
    in_counts = {}
    for n in legacy_notes:
        in_counts[n['type']] = in_counts.get(n['type'], 0) + 1
    lines = ["Equivalence report (legacy -> apiary):"]
    for t in sorted(set(in_counts) | set(counts)):
        lines.append(f"  {t:10s} in={in_counts.get(t, 0):4d}  out={counts.get(t, 0):4d}")
    lines.append(f"  {'ticket':10s} in={skipped:4d}  out=   0   (skipped, F2)")
    lines.append(f"  id-map entries: {len(id_map)}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a legacy .scribe/ store into apiary (read-only source).")
    ap.add_argument("legacy_dir", help="Path to the legacy per-repo .scribe/ directory")
    ap.add_argument("target", help="apiary target name or repo path")
    ap.add_argument("--dry-run", action="store_true",
                    help="Ingest into a scratch state-dir and print an equivalence report; write nothing to the target")
    ap.add_argument("--apiary-repo", default=None, help="Apiary checkout root (else auto-resolved)")
    args = ap.parse_args()

    legacy = Path(args.legacy_dir)
    if not legacy.is_dir():
        print(f"error: legacy dir not found: {legacy}", file=sys.stderr)
        return 1

    notes = read_legacy_notes(legacy)
    skipped = count_skipped(legacy)

    if args.dry_run:
        with tempfile.TemporaryDirectory() as td:
            store = ScribeStore(Path(td))
            id_map, counts = import_into(store, notes)
            print(equivalence_report(notes, counts, skipped, id_map))
            print("(dry-run: scratch state-dir; nothing written to the target)")
        return 0

    apiary_repo = Path(args.apiary_repo) if args.apiary_repo else None
    store = api.open_store(args.target, apiary_repo=apiary_repo)
    id_map, counts = import_into(store, notes)
    print(equivalence_report(notes, counts, skipped, id_map))
    return 0


if __name__ == "__main__":
    sys.exit(main())
