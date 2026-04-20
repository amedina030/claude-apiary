#!/usr/bin/env python3
"""One-shot retrofit: infer tags + areas for existing learnings.

Iterates every active learning in the scribe state directory, and for each
one that still lacks tags AND areas, calls `claude -p` to infer them from
content and writes them back as frontmatter (plus mirror into index.jsonl).

Idempotent — re-running skips entries that already have non-empty tags OR
areas, so partial failures are cheap to recover from.

Usage:
    python scripts/retrotag_learnings.py [--dry-run] [--model NAME] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from runner.claude_subprocess import run_claude
from scribe.notes import scribe_state_dir
from scribe.store import (
    ScribeStore,
    _format_learning_content,
    _LEARNING_FRONTMATTER_FIELDS,
    LEARNING_FOLDER,
    ARCHIVE_DIRNAME,
)

# Timeout per learning. Inference prompts are short (sub-KB) so anything
# over ~30s indicates network trouble rather than a slow model response.
PER_LEARNING_TIMEOUT = 60


def build_prompt(content: str, vocab: list[str]) -> str:
    """Single-turn prompt sent to `claude -p` for a learning.

    Mirrors _build_inference_prompt in scribe/notes.py so live learns and
    batch-retrotag produce consistent tag shapes. Kept separate (not
    imported) because scribe's copy is a private helper.
    """
    vocab_line = ', '.join(vocab) if vocab else '(none yet)'
    return (
        "You are tagging a project learning so it can be auto-surfaced when I later\n"
        "edit related files. Respond with a JSON object only — no prose, no markdown fence.\n\n"
        f"Existing tag vocabulary: {vocab_line}\n\n"
        'Return {"tags": [...], "areas": [...]} where:\n'
        '- tags: 1-3 short lowercase tokens (prefer existing vocabulary; invent only if needed).\n'
        '- areas: glob patterns matching file paths the learning applies to (e.g. "gui/**",\n'
        '  "scribe/notes.py", "core/hooks/*.py"). Empty list if not path-specific.\n\n'
        f"Learning content:\n{content}"
    )


def parse_inference_response(stdout: str) -> dict | None:
    """Extract {tags, areas} JSON from a `claude -p --output-format json`
    envelope. Returns None on any parse failure."""
    try:
        envelope = json.loads(stdout)
        inner = envelope.get('result', stdout) if isinstance(envelope, dict) else stdout
    except json.JSONDecodeError:
        inner = stdout
    if not isinstance(inner, str):
        return None
    text = inner.strip()
    fence = re.search(r'```(?:json)?\s*\n([\s\S]*?)```', text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        'tags': [str(t).strip() for t in data.get('tags', []) if str(t).strip()],
        'areas': [str(a).strip() for a in data.get('areas', []) if str(a).strip()],
    }


def collect_vocab(store: ScribeStore) -> list[str]:
    """Gather the current set of tags across all learnings for prompt context."""
    vocab: set[str] = set()
    for l in store.list_learnings():
        for t in l.get('tags') or []:
            if isinstance(t, str) and t.strip():
                vocab.add(t.strip())
    return sorted(vocab)


def write_back(store: ScribeStore, entry: dict, tags: list[str], areas: list[str]) -> None:
    """Rewrite a single learning's .md with frontmatter and update its
    index.jsonl entry in place. Uses the same _format_learning_content
    helper as fresh adds so diffs stay consistent.
    """
    year = entry['year']
    seq = entry['seq']
    full = store.get_learning(year, seq)
    body = (full.get('content') if full else '') or ''
    # Strip any frontmatter that would have been surfaced onto the dict —
    # `content` from get_learning is already body-only, so just re-prefix.
    frontmatter = {'tags': tags, 'areas': areas, 'supersedes': full.get('supersedes') if full else None}
    new_text = _format_learning_content(body, frontmatter)
    md_path = store._learning_dir() / str(year) / f'{seq}.md'
    md_path.write_text(new_text, encoding='utf-8')
    # Mirror into index.jsonl: load, replace entry, write back.
    year_dir = store._learning_dir() / str(year)
    idx = store._read_index(year_dir)
    for i, e in enumerate(idx):
        if e.get('seq') == seq:
            idx[i] = {**e, 'tags': tags, 'areas': areas}
            break
    store._write_index(year_dir, idx)


def retrotag(store: ScribeStore, *, dry_run: bool, model: str | None,
             limit: int | None) -> dict:
    """Process learnings in ascending display-ID order. Returns a report."""
    learnings = sorted(
        store.list_learnings(),
        key=lambda e: (e.get('year', 0), e.get('seq', 0)),
    )
    if limit:
        learnings = learnings[:limit]

    report = {
        'total': len(learnings),
        'processed': 0,
        'already_tagged': 0,
        'errors': [],
    }
    vocab = collect_vocab(store)

    for entry in learnings:
        display_id = entry.get('display_id', f"L-{entry.get('year')}-{entry.get('seq')}")
        tags = entry.get('tags') or []
        areas = entry.get('areas') or []
        if tags or areas:
            report['already_tagged'] += 1
            continue

        full = store.get_learning(entry['year'], entry['seq'])
        body = (full.get('content') if full else '') or ''
        if not body.strip():
            report['errors'].append(f'{display_id}: empty body, skipping')
            continue

        prompt = build_prompt(body, vocab)
        rc, stdout, stderr = run_claude(prompt, timeout=PER_LEARNING_TIMEOUT, model=model)
        if rc != 0:
            report['errors'].append(f'{display_id}: rc={rc} stderr={stderr[:200]!r}')
            continue
        parsed = parse_inference_response(stdout)
        if parsed is None:
            report['errors'].append(f'{display_id}: unparseable JSON')
            continue

        new_tags = parsed['tags']
        new_areas = parsed['areas']
        if dry_run:
            print(f'[dry-run] {display_id}: tags={new_tags} areas={new_areas}')
        else:
            try:
                write_back(store, entry, new_tags, new_areas)
                print(f'{display_id}: tags={new_tags} areas={new_areas}')
            except OSError as exc:
                report['errors'].append(f'{display_id}: write failed: {exc}')
                continue
            # Extend vocab so later prompts prefer the just-invented tags.
            for t in new_tags:
                if t not in vocab:
                    vocab.append(t)
        report['processed'] += 1

    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Print inferred tags/areas without writing')
    parser.add_argument('--model', default=None,
                        help='Override the claude model passed to `-p`')
    parser.add_argument('--limit', type=int, default=None,
                        help='Process only the first N learnings (useful for spot-checks)')
    args = parser.parse_args()

    state_dir = scribe_state_dir()
    if state_dir is None:
        print('error: not inside a git repo — cannot resolve scribe state dir', file=sys.stderr)
        sys.exit(1)

    store = ScribeStore(state_dir)
    t0 = time.monotonic()
    report = retrotag(store, dry_run=args.dry_run, model=args.model, limit=args.limit)
    elapsed = time.monotonic() - t0

    print()
    print(f'=== retrotag complete ({elapsed:.1f}s) ===')
    print(f'  total:          {report["total"]}')
    print(f'  processed:      {report["processed"]}')
    print(f'  already tagged: {report["already_tagged"]}')
    print(f'  errors:         {len(report["errors"])}')
    for err in report['errors'][:20]:
        print(f'    - {err}')
    if len(report['errors']) > 20:
        print(f'    ... and {len(report["errors"]) - 20} more')


if __name__ == '__main__':
    main()
