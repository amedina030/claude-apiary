#!/usr/bin/env python3
"""PreToolUse hook — inject top-3 relevant learnings before Edit/Write/Bash.

Matcher is a pure function (``score_learnings``) so the ranking logic is
fully unit-testable without touching stdin, subprocesses, or the scribe
state directory. The hook shell around it reads the PreToolUse payload,
resolves the scribe state dir from the session's cwd, loads
``learnings/<year>/index.jsonl``, and emits up to 3 matched learnings
as a single context_block.

Fail-open: any error path (missing payload field, corrupt index, state dir
not resolvable) degrades to an empty allow — the tool call still proceeds.
"""
from __future__ import annotations

import fnmatch
import re

# How many top-ranked learnings to inject. Kept at 3 to bound the context
# budget — the spec intentionally caps this rather than letting broad
# matches flood the pre-tool context.
TOP_N_INJECTED = 3

# Small recency bump applied to the N most-recent learnings so ties lean
# toward the freshest insight (usually the more-current workaround).
_RECENCY_WINDOW = 10
_RECENCY_BONUS = 0.05

# Score awarded when a tag substring matches a token from a Bash command.
# Lower than any real area match so path-based matches always win when both
# are available.
_TAG_MATCH_SCORE = 0.2

# Scores for area-glob matches. A "specific" glob (contains ``/``) — for
# example ``gui/web/**`` — scores higher than a bare wildcard glob like
# ``**`` so the most-specific known area wins when several match.
_SPECIFIC_AREA_SCORE = 1.0
_BROAD_AREA_SCORE = 0.5


def _area_is_specific(glob: str) -> bool:
    """An area glob is 'specific' when it pins down a path segment. Bare
    ``*`` / ``**`` globs or empty strings are treated as broad."""
    if not glob:
        return False
    if glob in ('*', '**'):
        return False
    return '/' in glob or not glob.startswith('*')


def _tokenize_command(command: str) -> list[str]:
    """Lowercase alphabetic tokens from a shell command — good enough for
    substring matching against tags. Strips flags, punctuation, and paths."""
    if not command:
        return []
    # Replace non-word runs with spaces, then split — keeps the implementation
    # dependency-free while discarding ``--flags``, quotes, and path separators.
    cleaned = re.sub(r'[^A-Za-z0-9_]+', ' ', command).lower()
    return [tok for tok in cleaned.split() if tok]


def _score_entry(entry: dict, *, target_path: str | None,
                 command_tokens: list[str]) -> tuple[float, str | None, str | None]:
    """Return ``(score, matched_area, matched_tag)`` for one learning.

    - ``matched_area`` is the glob that fired against ``target_path`` (or None).
    - ``matched_tag`` is the tag substring that fired against a command token.
    Used by the hook shell so the injected header can label *why* each
    learning surfaced.
    """
    score = 0.0
    matched_area: str | None = None
    matched_tag: str | None = None

    areas = entry.get('areas') or []
    if target_path and areas:
        for glob in areas:
            if not isinstance(glob, str) or not glob:
                continue
            if fnmatch.fnmatch(target_path, glob):
                candidate = _SPECIFIC_AREA_SCORE if _area_is_specific(glob) else _BROAD_AREA_SCORE
                if candidate > score:
                    score = candidate
                    matched_area = glob

    tags = entry.get('tags') or []
    if command_tokens and tags:
        for tag in tags:
            if not isinstance(tag, str) or not tag:
                continue
            tag_lower = tag.lower()
            for token in command_tokens:
                if tag_lower in token:
                    score += _TAG_MATCH_SCORE
                    if matched_tag is None:
                        matched_tag = tag
                    break

    return score, matched_area, matched_tag


def score_learnings(entries: list[dict], *,
                    target_path: str | None = None,
                    command: str | None = None,
                    top_n: int = TOP_N_INJECTED) -> list[dict]:
    """Rank ``entries`` by relevance to the current tool op and return top-N.

    Pure function — no I/O. Returns a new list of index-entry dicts
    decorated with ``_match_score``, ``_matched_area``, and ``_matched_tag``
    so the hook shell can render headers without re-scoring.

    Scoring tiebreakers, in order:
    1. Score descending (highest first).
    2. Recency bump — entries in the most-recent ``_RECENCY_WINDOW`` get a
       small bonus already baked into the raw score.
    3. Display-ID ascending — deterministic final tiebreak so two entries
       with identical score render in a stable order across runs.
    """
    if not entries or top_n <= 0:
        return []

    command_tokens = _tokenize_command(command or '')

    # Tie-breaking recency bump: sort by timestamp descending, take the top
    # _RECENCY_WINDOW, and add _RECENCY_BONUS to each of those entries'
    # scores. Doing it this way (instead of per-entry) avoids a pass over
    # the whole corpus when the caller just wants the fresh bump applied.
    by_time = sorted(
        entries,
        key=lambda e: e.get('timestamp', ''),
        reverse=True,
    )
    recent_ids = {id(e) for e in by_time[:_RECENCY_WINDOW]}

    scored: list[dict] = []
    for entry in entries:
        score, matched_area, matched_tag = _score_entry(
            entry, target_path=target_path, command_tokens=command_tokens,
        )
        if score <= 0:
            continue
        if id(entry) in recent_ids:
            score += _RECENCY_BONUS
        scored.append({
            **entry,
            '_match_score': score,
            '_matched_area': matched_area,
            '_matched_tag': matched_tag,
        })

    scored.sort(
        key=lambda e: (-e['_match_score'], e.get('display_id', '')),
    )
    return scored[:top_n]


def main():  # pragma: no cover — exercised by the hook-shell path
    """Entry point. Wrapped for fail-open behavior; never raises."""
    try:
        _run()
    except Exception:
        from core.hook_context import hook_allow
        hook_allow()


def _run():  # pragma: no cover — covered by integration in Step 7
    # Deferred imports keep cold-start cheap on the hot PreToolUse path.
    import os
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(project_root))

    from core.flags import is_enabled
    from core.hook_context import context_block, hook_allow, read_payload
    from core.sanitizer import sanitize_and_report
    from scribe.notes import _git_repo_root, scribe_state_dir
    from scribe.store import ScribeStore

    if not is_enabled('learnings-inject'):
        hook_allow()
        return

    if os.environ.get('APIARY_RUNNER_SUBPROCESS') == '1':
        hook_allow()
        return

    payload = read_payload()
    tool_name = payload.get('tool_name') or ''
    if tool_name not in ('Edit', 'Write', 'Bash'):
        hook_allow()
        return

    tool_input = payload.get('tool_input') or {}
    if not isinstance(tool_input, dict):
        hook_allow()
        return

    target_path = None
    command = None
    if tool_name in ('Edit', 'Write'):
        target_path = tool_input.get('file_path') or ''
        if not target_path:
            hook_allow()
            return
        target_path = _normalize_for_glob(target_path, payload.get('cwd'))
    else:  # Bash
        command = tool_input.get('command') or ''
        if not command:
            hook_allow()
            return

    cwd = payload.get('cwd') or str(project_root)
    state_dir = scribe_state_dir(Path(cwd))
    if state_dir is None:
        hook_allow()
        return

    try:
        store = ScribeStore(state_dir)
        entries = store.list_learnings()
    except Exception:
        hook_allow()
        return

    top = score_learnings(entries, target_path=target_path, command=command)
    if not top:
        hook_allow()
        return

    blocks: list[str] = []
    for entry in top:
        full = store.get_learning(entry['year'], entry['seq'])
        if not full:
            continue
        body = full.get('content') or entry.get('summary', '')
        if not body:
            continue
        header_bits = [f"L-{entry.get('year', '?')}-{entry.get('seq', '?')}"]
        if entry.get('_matched_area'):
            header_bits.append(f"matched area: {entry['_matched_area']}")
        if entry.get('_matched_tag'):
            header_bits.append(f"matched tag: {entry['_matched_tag']}")
        scrubbed, _hits = sanitize_and_report(body)
        blocks.append(f"--- relevant learning ({' · '.join(header_bits)}) ---\n{scrubbed}")

    if not blocks:
        hook_allow()
        return

    hook_allow(context_block('learnings', *blocks))


def _normalize_for_glob(file_path: str, cwd: str | None) -> str:  # pragma: no cover
    """Return a repo-relative, forward-slash path for fnmatch.

    Learning area globs are written in repo-relative POSIX form (``gui/**``,
    ``scribe/notes.py``), so an absolute Windows path like
    ``D:\\Professional\\claude-apiary\\gui\\web\\app.js`` must be trimmed
    to ``gui/web/app.js`` before fnmatch can see it.
    """
    from pathlib import Path, PurePosixPath
    p = Path(file_path)
    if cwd:
        try:
            p = Path(p).resolve().relative_to(Path(cwd).resolve())
        except (ValueError, OSError):
            pass
    return PurePosixPath(*p.parts).as_posix()


if __name__ == '__main__':  # pragma: no cover
    main()
