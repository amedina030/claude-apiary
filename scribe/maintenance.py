"""Scribe maintenance — the operations that treat the store as a whole.

`repair`, `backfill-brief`, `backup`, `restore` and `mark-reviewed` all walk
every type folder rather than acting on one note, and none of them belongs in
an argparse handler. They live here as functions that take a store (or a
state dir) and return a report; `scribe/notes.py` supplies the flags and does
the printing.

The folder walk is written once, in :func:`iter_index_folders`, because the
two copies it replaced had drifted: `repair` and `backfill-brief` disagreed
about whether a year directory without an ``archive/`` subfolder was worth
visiting.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.utils.atomic import write_text_atomic
from scribe.store import (
    ARCHIVE_DIRNAME,
    INDEX_FILENAME,
    LEARNING_FOLDER,
    NEXT_SEQ_FILENAME,
    TYPE_FOLDERS,
    TYPE_PREFIXES,
    ScribeStore,
    derive_brief_summary,
    derive_summary,
)

#: Every folder scribe manages, type folders plus learnings.
ALL_FOLDER_NAMES: list[str] = list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]

#: Where dated index snapshots live, under the scribe state dir.
BACKUPS_DIRNAME = 'backups'
#: How many dated snapshots `backup` keeps by default.
DEFAULT_RETAIN = 30

_FOLDER_TO_TYPE: dict[str, str] = {v: k for k, v in TYPE_FOLDERS.items()}


def folder_to_note_type(folder_name: str) -> str:
    """Invert ``TYPE_FOLDERS``: ``'todos'`` → ``'todo'``.

    ``learnings`` maps to ``learning``; anything unrecognised falls back to
    ``general`` so a stray directory cannot crash a repair run.
    """
    if folder_name == LEARNING_FOLDER:
        return 'learning'
    return _FOLDER_TO_TYPE.get(folder_name, 'general')


@dataclass(frozen=True)
class IndexFolder:
    """One index.jsonl's worth of notes: where it is and what lives in it."""
    note_type: str
    year: int
    year_dir: Path
    folder: Path
    is_archive: bool


def iter_index_folders(state_dir: Path):
    """Yield every :class:`IndexFolder` under *state_dir*, active then archive.

    Skips folders that do not exist and year directories whose name is not
    all digits, so a ``templates/`` or ``backups/`` sibling is never mistaken
    for note storage.
    """
    for folder_name in ALL_FOLDER_NAMES:
        type_dir = state_dir / folder_name
        if not type_dir.exists():
            continue
        note_type = folder_to_note_type(folder_name)
        for child in sorted(type_dir.iterdir()):
            if not child.is_dir() or not child.name.isdigit():
                continue
            for is_archive in (False, True):
                folder = child / ARCHIVE_DIRNAME if is_archive else child
                if not folder.exists():
                    continue
                yield IndexFolder(note_type, int(child.name), child, folder, is_archive)


def has_any_data(state_dir: Path) -> bool:
    """True when *state_dir* holds at least one of scribe's managed folders."""
    return state_dir.exists() and any(
        (state_dir / name).exists() for name in ALL_FOLDER_NAMES)


# --------------------------------------------------------------------------- #
# repair
# --------------------------------------------------------------------------- #

def _prefix(dry_run: bool) -> str:
    """The ``(dry-run) `` marker every report headline shares."""
    return '(dry-run) ' if dry_run else ''


@dataclass
class RepairReport:
    """What a repair pass found (and, unless dry-run, fixed)."""
    rebuilt: int = 0
    orphans: int = 0
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    found_data: bool = True

    def summary(self, dry_run: bool = False) -> str:
        return (f'Repair {_prefix(dry_run)}complete: {self.rebuilt} entries rebuilt, '
                f'{self.orphans} orphans {"detected" if dry_run else "removed"}')


def _rebuilt_entry(folder: Path, note_type: str, year: int, seq: int,
                   is_archive: bool) -> dict:
    """Reconstruct an index row from a body file that lost its entry.

    ``session`` is empty and ``timestamp`` comes from the file's mtime —
    both are the best that survives when the index row is gone.
    """
    content = (folder / f'{seq}.md').read_text(encoding='utf-8')
    mtime = (folder / f'{seq}.md').stat().st_mtime
    return {
        'display_id': f"{TYPE_PREFIXES.get(note_type, 'G')}-{year}-{seq}",
        'type': note_type,
        'year': year,
        'seq': seq,
        'status': 'archived' if is_archive else 'active',
        'session': '',
        'timestamp': datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        'summary': derive_summary(content),
        'brief_summary': derive_brief_summary(content),
        'has_body': bool(content),
    }


def _body_seqs(folder: Path, report: RepairReport) -> set:
    """The seq numbers with a body file in *folder*; warns on odd filenames."""
    seqs = set()
    for md_path in folder.glob('*.md'):
        try:
            seqs.add(int(md_path.stem))
        except ValueError:
            report.warnings.append(
                f'Warning: skipping non-integer filename {md_path.name} in {folder}')
    return seqs


def _repair_next_seq(spot: IndexFolder, entries: list, state_dir: Path,
                     dry_run: bool, report: RepairReport) -> None:
    """Reset a year's ``next_seq`` to one past the highest seq it holds.

    Counts the archive too: an archived note's seq is spent, and reissuing it
    would collide with a body file that still exists.
    """
    max_seq = max((e.get('seq', 0) for e in entries if isinstance(e.get('seq'), int)),
                  default=0)
    arc_dir = spot.year_dir / ARCHIVE_DIRNAME
    if arc_dir.exists():
        for entry in ScribeStore._read_index(arc_dir):
            seq = entry.get('seq', 0)
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
    new_next_seq = max_seq + 1
    seq_path = spot.year_dir / NEXT_SEQ_FILENAME
    current_seq = 1
    if seq_path.exists():
        try:
            current_seq = int(seq_path.read_text(encoding='utf-8').strip())
        except ValueError:
            current_seq = 1
    if new_next_seq == current_seq:
        return
    if not dry_run:
        write_text_atomic(seq_path, str(new_next_seq))
    report.lines.append(
        f'  * next_seq for {spot.year_dir.relative_to(state_dir)}: '
        f'{current_seq} -> {new_next_seq}')


def repair(store: ScribeStore, *, dry_run: bool = False) -> RepairReport:
    """Reconcile every index against the body files beside it.

    Two directions: a ``<seq>.md`` with no index row is rebuilt from the file
    (mtime as timestamp, empty session); an index row with no ``<seq>.md`` is
    an orphan and is dropped. Then each year's ``next_seq`` is reset to one
    past the highest seq it holds, archive included.
    """
    state_dir = store.state_dir
    report = RepairReport()
    if not has_any_data(state_dir):
        report.found_data = False
        return report

    for spot in iter_index_folders(state_dir):
        entries = ScribeStore._read_index(spot.folder)
        indexed_seqs = {e.get('seq') for e in entries if isinstance(e.get('seq'), int)}
        md_seqs = _body_seqs(spot.folder, report)

        new_entries = list(entries)
        for seq in sorted(md_seqs - indexed_seqs):
            new_entries.append(
                _rebuilt_entry(spot.folder, spot.note_type, spot.year, seq, spot.is_archive))
            report.rebuilt += 1
            report.lines.append(
                f'  + rebuilt entry {TYPE_PREFIXES.get(spot.note_type, "G")}-'
                f'{spot.year}-{seq} in {spot.folder.relative_to(state_dir)}')

        kept = []
        for entry in new_entries:
            seq = entry.get('seq')
            if isinstance(seq, int) and seq not in md_seqs:
                report.orphans += 1
                report.lines.append(
                    f'  - orphan entry seq={seq} in {spot.folder.relative_to(state_dir)}')
            else:
                kept.append(entry)

        if not dry_run and len(kept) != len(entries):
            ScribeStore._write_index(spot.folder, kept)

        if not spot.is_archive:
            _repair_next_seq(spot, kept, state_dir, dry_run, report)

    return report


# --------------------------------------------------------------------------- #
# backfill-brief
# --------------------------------------------------------------------------- #

@dataclass
class BackfillReport:
    """What a brief_summary backfill changed."""
    updated: int = 0
    already_set: int = 0
    lines: list[str] = field(default_factory=list)

    def summary(self, dry_run: bool = False) -> str:
        return (f'Backfill {_prefix(dry_run)}complete: {self.updated} updated, '
                f'{self.already_set} already set.')


def backfill_brief(store: ScribeStore, *, dry_run: bool = False,
                   force: bool = False) -> BackfillReport:
    """Derive ``brief_summary`` for every entry that lacks one.

    A one-shot migration that stays safe to re-run: only entries with a
    missing or empty brief are touched, unless *force* re-derives all of
    them. Entries whose body file is gone fall back to the stored summary.
    """
    report = BackfillReport()
    for spot in iter_index_folders(store.state_dir):
        entries = ScribeStore._read_index(spot.folder)
        changed = False
        for entry in entries:
            seq = entry.get('seq')
            if not isinstance(seq, int):
                continue
            existing = (entry.get('brief_summary') or '').strip()
            if existing and not force:
                report.already_set += 1
                continue
            md_path = spot.folder / f'{seq}.md'
            source = (md_path.read_text(encoding='utf-8', errors='replace')
                      if md_path.exists() else entry.get('summary', ''))
            derived = derive_brief_summary(source)
            if derived == existing:
                report.already_set += 1
                continue
            entry['brief_summary'] = derived
            report.updated += 1
            changed = True
            report.lines.append(f'  + {entry.get("display_id", "?")}: {derived[:60]!r}')
        if changed and not dry_run:
            ScribeStore._write_index(spot.folder, entries)
    return report


# --------------------------------------------------------------------------- #
# mark-reviewed
# --------------------------------------------------------------------------- #

def mark_reviewed(store: ScribeStore) -> Path:
    """Stamp ``<scribe>/learnings/last_review`` and return its path.

    The file's mtime is what ``core.startup._review_staleness_marker``
    compares against the 30-day threshold; the contents are irrelevant.
    Raises ``OSError`` — the caller decides how loudly to fail.
    """
    marker = store.state_dir / LEARNING_FOLDER / 'last_review'
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('', encoding='utf-8')
    os.utime(marker)
    return marker


# --------------------------------------------------------------------------- #
# backup / restore
# --------------------------------------------------------------------------- #

def backups_root(state_dir: Path) -> Path:
    """The directory dated snapshots live in."""
    return Path(state_dir) / BACKUPS_DIRNAME


def list_backups(state_dir: Path) -> list:
    """Dated snapshot directories under *state_dir*, oldest first."""
    root = backups_root(state_dir)
    if not root.exists():
        return []
    return sorted((d for d in root.iterdir() if d.is_dir()), key=lambda p: p.name)


def create_backup(state_dir: Path, root: Path, date_str: str) -> tuple:
    """Copy every ``index.jsonl`` under *state_dir* into ``root/date_str``.

    Bodies and ``next_seq`` are deliberately not copied: the indexes are the
    fragile part (they are rewritten whole on every mutation), and a restore
    plus :func:`repair` rebuilds both from the bodies that never moved.
    Replaces an existing snapshot for the same date. Returns
    ``(target_dir, files_copied)``.
    """
    target = root / date_str
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for spot in iter_index_folders(Path(state_dir)):
        src = spot.folder / INDEX_FILENAME
        if not src.exists():
            continue
        dst = target / spot.folder.relative_to(state_dir)
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst / INDEX_FILENAME)
        count += 1
    return (target, count)


def prune_backups(root: Path, retain: int) -> list:
    """Delete all but the *retain* newest snapshots; ``0`` keeps only the newest."""
    if not root.exists():
        return []
    all_dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda p: p.name)
    if retain == 0:
        to_delete = all_dirs[:-1] if len(all_dirs) > 1 else []
    else:
        to_delete = all_dirs[:-retain] if len(all_dirs) > retain else []
    for d in to_delete:
        shutil.rmtree(d)
    return to_delete


# --------------------------------------------------------------------------- #
# retrotag
# --------------------------------------------------------------------------- #

@dataclass
class RetrotagReport:
    """What a retrotag pass tagged, skipped, and failed on."""
    total: int = 0
    processed: int = 0
    already_tagged: int = 0
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self, dry_run: bool = False) -> str:
        return (f'Retrotag {_prefix(dry_run)}complete: {self.processed} tagged, '
                f'{self.already_tagged} already tagged, {len(self.errors)} error(s) '
                f'of {self.total} learning(s).')


def retrotag(store: ScribeStore, *, dry_run: bool = False,
             model: "str | None" = None, limit: "int | None" = None,
             timeout: "int | None" = None) -> RetrotagReport:
    """Infer tags and areas for every learning that has neither.

    Idempotent by construction: an entry with any tag or any area is left
    alone, so a run that dies halfway costs only the calls it already made.
    Walks in display-ID order and feeds each newly invented tag back into the
    vocabulary, so later learnings reuse what earlier ones just established.

    Unlike ``learn``, this never consults the inference switch — inference is
    the whole command.
    """
    from scribe import infer  # local: pulls the runner subprocess wrapper

    report = RetrotagReport()
    learnings = sorted(store.list_learnings(),
                       key=lambda e: (e.get('year', 0), e.get('seq', 0)))
    if limit:
        learnings = learnings[:limit]
    report.total = len(learnings)
    vocab = infer.collect_vocab(store)

    for entry in learnings:
        display_id = entry.get('display_id', f"L-{entry.get('year')}-{entry.get('seq')}")
        if (entry.get('tags') or []) or (entry.get('areas') or []):
            report.already_tagged += 1
            continue

        full = store.get_learning(entry['year'], entry['seq'])
        body = (full.get('content') if full else '') or ''
        if not body.strip():
            report.errors.append(f'{display_id}: empty body, skipping')
            continue

        inferred = infer.infer_tags_areas(
            body, store, model=model, vocab=vocab,
            timeout=timeout or infer.RETROTAG_TIMEOUT,
            warn=lambda msg, _id=display_id: report.errors.append(f'{_id}: {msg}'))
        if not inferred:
            continue

        tags, areas = inferred['tags'], inferred['areas']
        report.lines.append(f'  {display_id}: tags={tags} areas={areas}')
        if not dry_run:
            try:
                store.update_learning(entry['year'], entry['seq'], tags=tags, areas=areas)
            except OSError as exc:
                report.errors.append(f'{display_id}: write failed: {exc}')
                continue
            for tag in tags:
                if tag not in vocab:
                    vocab.append(tag)
        report.processed += 1

    return report


@dataclass
class RestoreReport:
    """What a restore copied back, and from where."""
    source: "Path | None" = None
    restored: int = 0
    lines: list[str] = field(default_factory=list)

    def summary(self, dry_run: bool = False) -> str:
        where = self.source.name if self.source else '(nothing)'
        return (f'Restore {_prefix(dry_run)}complete: {self.restored} index file(s) '
                f'from {where}.')


def restore_backup(state_dir: Path, date_str: "str | None" = None, *,
                   dry_run: bool = False) -> RestoreReport:
    """Copy the indexes from a dated snapshot back over the live ones.

    *date_str* defaults to the newest snapshot. Restoring an index older than
    the bodies beside it is safe by construction: a body with no row is not
    lost, it is what :func:`repair` rebuilds — which is why the caller is told
    to run repair afterwards. Raises ``FileNotFoundError`` when there is no
    snapshot to restore from.
    """
    snapshots = list_backups(state_dir)
    if not snapshots:
        raise FileNotFoundError(f'no snapshots under {backups_root(state_dir)}')
    if date_str:
        matches = [d for d in snapshots if d.name == date_str]
        if not matches:
            raise FileNotFoundError(
                f'no snapshot dated {date_str}; have '
                f'{", ".join(d.name for d in snapshots)}')
        source = matches[0]
    else:
        source = snapshots[-1]

    report = RestoreReport(source=source)
    for src in sorted(source.rglob(INDEX_FILENAME)):
        rel = src.relative_to(source)
        dst = Path(state_dir) / rel
        report.restored += 1
        report.lines.append(f'  + {rel.as_posix()}')
        if not dry_run:
            write_text_atomic(dst, src.read_text(encoding='utf-8'))
    return report
