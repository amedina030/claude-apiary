#!/usr/bin/env python3
"""PreToolUse hook — inject top-3 relevant learnings before Edit/Write/Bash.

Matcher is a pure function (``score_learnings``) so the ranking logic is
fully unit-testable without touching stdin, subprocesses, or the scribe
state directory. The hook shell around it reads the PreToolUse payload,
resolves the scribe state dir from the session's cwd, loads
``learnings/<year>/index.jsonl``, and emits up to 3 matched learnings
as a single context_block.

Matching (2026-09-05 tightening): Edit/Write match the file path against each
learning's ``areas`` globs; Bash matches the command's *whole tokens* against
each learning's ``tags`` after stripping the launcher idiom
(``$(git rev-parse --show-toplevel)/.claude/apiary/launch.py``). Substring
matching used to let ``api`` fire on "apiary", ``js`` on "json" and ``git`` on
every launcher call, so top-3 was recency among ~30 candidates, not relevance.

Runner subprocesses (``APIARY_RUNNER_SUBPROCESS=1``) are deliberately *not*
skipped here, unlike the startup dump: a headless executor editing a tagged
area is exactly where a known-trap note pays for its ~1KB.

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

# Score awarded when a tag matches a Bash command token whole (every token
# of a hyphenated tag must be present). Lower than any real area match so
# path-based matches always win when both are available.
_TAG_MATCH_SCORE = 0.2

# Boilerplate every launcher invocation carries. Stripped before tokenizing
# so `git`, `claude`, `apiary` and `launch` are not tokens of every command.
_LAUNCHER_BOILERPLATE_RE = re.compile(
    r"\$\(\s*git\s+rev-parse\s+--show-toplevel\s*\)"
    r"|\$CLAUDE_PROJECT_DIR"
    r"|[^\s\"']*[\\/]\.claude[\\/]apiary[\\/]launch\.py",
    re.IGNORECASE,
)
_TOKEN_SPLIT_RE = re.compile(r"[^A-Za-z0-9_]+")

# Scores for area-glob matches. A "specific" glob (contains ``/``) — for
# example ``gui/web/**`` — scores higher than a bare wildcard glob like
# ``**`` so the most-specific known area wins when several match.
_SPECIFIC_AREA_SCORE = 1.0
_BROAD_AREA_SCORE = 0.5


def _area_is_specific(glob: str) -> bool:
    """An area glob is 'specific' when it pins down a path segment.

    At least one ``/``-separated segment must be wildcard-free: ``gui/**``
    and ``scribe/notes.py`` are specific; ``**/*.py`` — every segment a
    wildcard — matches most of any repo and would otherwise outrank
    subsystem globs on every ``.py`` edit. Bare ``*`` / ``**`` and empty
    strings are broad.
    """
    if not glob:
        return False
    segments = [seg for seg in glob.split("/") if seg]
    if not segments:
        return False
    return any(not any(ch in seg for ch in "*?[") for seg in segments)


def seen_ids_path(repo_root, session_id: str, agent_id: str | None = None):
    """Session-scoped dedup file: one injected display_id per line.

    Lives with the other per-session flags under
    ``<repo>/.claude/apiary/session-tmp/`` (the ``inject_session``
    convention), so the age-based sweep that cleans those files covers
    this one too. A subagent shares the parent's ``session_id`` but not its
    context, so when the payload names an ``agent_id`` the file is keyed by
    both — otherwise a learning the parent already saw would be withheld
    from a worker that never did.
    """
    from pathlib import Path

    stem = f"{session_id}_{agent_id}" if agent_id else session_id
    return Path(repo_root) / ".claude" / "apiary" / "session-tmp" / f"{stem}_learnings_injected"


def load_seen_ids(path) -> set:
    """IDs already injected this session. Missing/unreadable file → empty."""
    try:
        return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}
    except OSError:
        return set()


def record_seen_ids(path, ids) -> None:
    """Append newly injected IDs. Best-effort: failure must not block injection."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for injected_id in ids:
                fh.write(f"{injected_id}\n")
    except OSError:
        pass


# Session ids come from the hook payload (external input) and end up in a
# filename — accept only the uuid-ish shape Claude Code actually sends.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}\Z")


def _tokenize_command(command: str) -> list[str]:
    """Lowercase word tokens from a shell command, launcher boilerplate removed.

    Strips flags, punctuation and path separators; a path contributes its
    segments as separate tokens (``scribe/notes.py`` → ``scribe``, ``notes``,
    ``py``), which is what lets a ``scribe`` tag fire on a scribe command.
    """
    if not command:
        return []
    cleaned = _LAUNCHER_BOILERPLATE_RE.sub(" ", command)
    cleaned = _TOKEN_SPLIT_RE.sub(" ", cleaned).lower()
    return [tok for tok in cleaned.split() if tok]


def _tag_tokens(tag: str) -> list[str]:
    """A tag's word tokens, so ``claude-code`` matches the tokens ``claude``
    and ``code`` rather than the literal hyphenated string no command has."""
    return [tok for tok in _TOKEN_SPLIT_RE.sub(" ", tag).lower().split() if tok]


def _score_entry(
    entry: dict, *, target_path: str | None, command_tokens: list[str]
) -> tuple[float, str | None, str | None]:
    """Return ``(score, matched_area, matched_tag)`` for one learning.

    - ``matched_area`` is the glob that fired against ``target_path`` (or None).
    - ``matched_tag`` is the tag whose every token appeared in the command.
    Used by the hook shell so the injected header can label *why* each
    learning surfaced.
    """
    score = 0.0
    matched_area: str | None = None
    matched_tag: str | None = None
    token_set = set(command_tokens)

    areas = entry.get("areas") or []
    if target_path and areas:
        for glob in areas:
            if not isinstance(glob, str) or not glob:
                continue
            if fnmatch.fnmatch(target_path, glob):
                candidate = _SPECIFIC_AREA_SCORE if _area_is_specific(glob) else _BROAD_AREA_SCORE
                if candidate > score:
                    score = candidate
                    matched_area = glob

    tags = entry.get("tags") or []
    if token_set and tags:
        for tag in tags:
            if not isinstance(tag, str) or not tag:
                continue
            parts = _tag_tokens(tag)
            # Whole-token, all parts: ``api`` must not fire on "apiary" nor
            # ``js`` on "json" — that substring leak made every launcher call
            # match ~30 learnings and reduced top-3 to a recency sort.
            if parts and all(part in token_set for part in parts):
                score += _TAG_MATCH_SCORE
                if matched_tag is None:
                    matched_tag = tag

    return score, matched_area, matched_tag


def score_learnings(
    entries: list[dict],
    *,
    target_path: str | None = None,
    command: str | None = None,
    top_n: int = TOP_N_INJECTED,
) -> list[dict]:
    """Rank ``entries`` by relevance to the current tool op and return top-N.

    Pure function — no I/O. Returns a new list of index-entry dicts
    decorated with ``_match_score``, ``_matched_area``, and ``_matched_tag``
    so the hook shell can render headers without re-scoring.

    Scoring tiebreakers, in order:
    1. Score descending (highest first).
    2. Recency bump — entries in the most-recent ``_RECENCY_WINDOW`` get a
       small bonus already baked into the raw score.
    3. Timestamp descending, then display-ID descending — newest first,
       deterministic across runs.
    """
    if not entries or top_n <= 0:
        return []

    command_tokens = _tokenize_command(command or "")

    # Tie-breaking recency bump: sort by timestamp descending, take the top
    # _RECENCY_WINDOW, and add _RECENCY_BONUS to each of those entries'
    # scores. Doing it this way (instead of per-entry) avoids a pass over
    # the whole corpus when the caller just wants the fresh bump applied.
    by_time = sorted(
        entries,
        key=lambda e: e.get("timestamp", ""),
        reverse=True,
    )
    recent_ids = {id(e) for e in by_time[:_RECENCY_WINDOW]}

    scored: list[dict] = []
    for entry in entries:
        score, matched_area, matched_tag = _score_entry(
            entry,
            target_path=target_path,
            command_tokens=command_tokens,
        )
        if score <= 0:
            continue
        if id(entry) in recent_ids:
            score += _RECENCY_BONUS
        scored.append(
            {
                **entry,
                "_match_score": score,
                "_matched_area": matched_area,
                "_matched_tag": matched_tag,
            }
        )

    # Newest-first tiebreak: a fresh learning usually carries the current
    # workaround; display-id-ascending handed every tie to the repo's oldest
    # notes. Two stable sorts: recency first, then score.
    scored.sort(
        key=lambda e: (e.get("timestamp", ""), e.get("display_id", "")),
        reverse=True,
    )
    scored.sort(key=lambda e: -e["_match_score"])
    return scored[:top_n]


def run(payload: dict):  # pragma: no cover — covered by integration; the
    #                       ranking logic lives in score_learnings (unit-tested).
    """Return the top-3 relevant learnings for this Edit/Write/Bash call."""
    # Deferred imports keep cold-start cheap on the hot PreToolUse path.
    import os
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(project_root))

    from core.flags import is_enabled
    from core.hook_context import HookResult, context_block
    from core.sanitizer import sanitize_and_report

    # scribe.paths, not scribe.notes: this runs before every Edit/Write/Bash,
    # and importing the CLI would pull argparse plus the maintenance and
    # inference modules onto the hot path for a single path lookup.
    from scribe.paths import scribe_state_dir
    from scribe.store import ScribeStore

    # Default-on (2026-09): the per-repo kill switch is
    # `core/flags.py enable learnings-inject-off`.
    if is_enabled("learnings-inject-off"):
        return None

    # No APIARY_RUNNER_SUBPROCESS short-circuit here, on purpose: the runner
    # skip exists for the tens-of-KB startup dump, and a headless executor
    # editing a tagged area is where a ~1KB known-trap note earns its keep.

    tool_name = payload.get("tool_name") or ""
    if tool_name not in ("Edit", "Write", "Bash"):
        return None

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    target_path = None
    command = None
    if tool_name in ("Edit", "Write"):
        target_path = tool_input.get("file_path") or ""
        if not target_path:
            return None
        target_path = _normalize_for_glob(target_path, payload.get("cwd"))
    else:  # Bash
        command = tool_input.get("command") or ""
        if not command:
            return None

    cwd = payload.get("cwd") or str(project_root)
    state_dir = scribe_state_dir(Path(cwd))
    if state_dir is None:
        return None

    try:
        store = ScribeStore(state_dir)
        entries = store.list_learnings()
    except Exception:
        return None

    top = score_learnings(entries, target_path=target_path, command=command)
    if not top:
        return None

    # Per-session dedup: without it the same top-3 ride along on every
    # matching call (every `git ...` command re-injects the same git notes).
    session_id = str(payload.get("session_id") or "").strip()
    seen_path = None
    if _SESSION_ID_RE.match(session_id):
        # Subagent tool calls carry the parent's session_id; key their
        # seen-file by agent_id too when the payload names one, so the
        # parent's history does not starve a worker with fresh context.
        agent_id = str(payload.get("agent_id") or "").strip()
        if not _SESSION_ID_RE.match(agent_id):
            agent_id = ""
        repo_root = os.environ.get("CLAUDE_PROJECT_DIR", "").strip() or cwd
        seen_path = seen_ids_path(repo_root, session_id, agent_id or None)
        seen = load_seen_ids(seen_path)
        top = [e for e in top if e.get("display_id", "") not in seen]
        if not top:
            return None

    blocks: list[str] = []
    injected_ids: list[str] = []
    for entry in top:
        full = store.get_learning(entry["year"], entry["seq"])
        if not full:
            continue
        body = full.get("content") or entry.get("summary", "")
        if not body:
            continue
        header_bits = [f"L-{entry.get('year', '?')}-{entry.get('seq', '?')}"]
        if entry.get("_matched_area"):
            header_bits.append(f"matched area: {entry['_matched_area']}")
        if entry.get("_matched_tag"):
            header_bits.append(f"matched tag: {entry['_matched_tag']}")
        scrubbed, _hits = sanitize_and_report(body)
        blocks.append(f"--- relevant learning ({' · '.join(header_bits)}) ---\n{scrubbed}")
        if entry.get("display_id"):
            injected_ids.append(entry["display_id"])

    if not blocks:
        return None

    if seen_path is not None and injected_ids:
        record_seen_ids(seen_path, injected_ids)

    return HookResult(context=context_block("learnings", *blocks))


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


if __name__ == "__main__":  # pragma: no cover
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.hook_context import run_standalone

    run_standalone(run)
