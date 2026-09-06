"""Turn-pair capture for the compass rule table (D-2026-62, step 1).

The one hard fact in a transcript is "Claude said A, the user replied B". This
module turns a Claude Code session JSONL into ``(assistant_text, user_turn)``
pairs and appends them to ``<state-dir>/compass/turns/<sid>.jsonl`` so the
classifier (``compass/classify.py``) can read them later, after Claude Code
has pruned the transcript itself (L-2026-180).

It is called from the Stop hook ``core/hooks/compass_pair_log.py`` at the end
of every assistant turn, so it must be cheap: a cursor file beside the turns
file records the byte offset already consumed and the assistant text of the
turn that just finished (the "carry"), and each call reads only what the
transcript grew by. No model call happens here.

Record filter (L-2026-87, L-2026-172 and the 2026-09-06 field census):

- a **user prompt** is ``type == "user"`` with a ``promptId``, no
  ``attachment`` key, ``message.content`` a non-empty string, not a sidechain,
  not ``entrypoint == "sdk-cli"`` (headless ``claude -p``), not a
  task-notification (``origin.kind != "human"`` / ``promptSource == "system"``)
  and not a slash-command invocation (``<command-name>`` / ``<local-command``);
- **assistant text** is every ``type == "assistant"`` record's ``text`` blocks
  (tool_use blocks dropped), with the same sidechain / sdk-cli exclusions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from compass import store
from core.utils.atomic import write_json_atomic
from core.utils.filelock import FileLock

#: Keep the tail of the assistant text — the recommendation / summary the user
#: is reacting to sits at the end — and the head of the user turn.
ASSISTANT_MAX_CHARS = 2500
USER_MAX_CHARS = 1500
#: The carry is persisted between Stop calls; bound it so the cursor stays small.
CARRY_MAX_CHARS = 8000

_SLASH_PREFIXES = ("<command-name>", "<local-command")


# ---------------------------------------------------------------------------
# Record filter
# ---------------------------------------------------------------------------


def _excluded_record(record: dict) -> bool:
    """Sidechain (subagent) and headless records never carry the user's voice."""
    if record.get("isSidechain") is True:
        return True
    return record.get("entrypoint") == "sdk-cli"


def user_prompt_text(record: dict) -> str | None:
    """The user's own words for a prompt record, else ``None``."""
    if not isinstance(record, dict) or record.get("type") != "user":
        return None
    if "promptId" not in record or "attachment" in record:
        return None
    if _excluded_record(record):
        return None
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    if record.get("promptSource") == "system":
        return None
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith(_SLASH_PREFIXES):
        return None
    return text


def assistant_text(record: dict) -> str | None:
    """The joined ``text`` blocks of an assistant record, else ``None``."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    if _excluded_record(record):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if not isinstance(content, list):
        return None
    parts = [
        str(block.get("text", "")).strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "\n\n".join(p for p in parts if p)
    return text or None


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "..." + text[-(limit - 3) :]


def _head(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Pair extraction over an ordered record stream
# ---------------------------------------------------------------------------


@dataclass
class PairState:
    """What a walk over the records carries from one call to the next."""

    carry: str | None = None  # assistant text of the last finished turn
    carry_ts: str | None = None
    seen_prompt_ids: set[str] = field(default_factory=set)


def extract_pairs(records: list[dict], state: PairState) -> list[dict]:
    """Pair every user prompt with the assistant text that preceded it.

    Mutates *state*: on return ``state.carry`` holds the assistant text
    accumulated since the last user prompt (this turn's reply), which becomes
    the ``assistant`` half of the next pair. A user prompt with nothing before
    it (the first turn of a session) yields no pair — the classifier never
    reads a user turn alone.
    """
    pairs: list[dict] = []
    acc: list[str] = [state.carry] if state.carry else []
    acc_ts = state.carry_ts
    for record in records:
        text = user_prompt_text(record)
        if text is not None:
            prompt_id = str(record.get("promptId"))
            previous = "\n\n".join(acc).strip()
            if previous and prompt_id not in state.seen_prompt_ids:
                pairs.append(
                    {
                        "prompt_id": prompt_id,
                        "ts": record.get("timestamp"),
                        "assistant_ts": acc_ts,
                        "assistant": _tail(previous, ASSISTANT_MAX_CHARS),
                        "user": _head(text, USER_MAX_CHARS),
                    }
                )
            state.seen_prompt_ids.add(prompt_id)
            acc = []
            acc_ts = None
            continue
        reply = assistant_text(record)
        if reply is not None:
            acc.append(reply)
            acc_ts = record.get("timestamp") or acc_ts
    joined = "\n\n".join(acc).strip()
    state.carry = _tail(joined, CARRY_MAX_CHARS) if joined else None
    state.carry_ts = acc_ts if joined else None
    return pairs


# ---------------------------------------------------------------------------
# Incremental update driven by the Stop hook
# ---------------------------------------------------------------------------


def _load_cursor(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _existing_prompt_ids(turns_file: Path) -> set[str]:
    ids: set[str] = set()
    try:
        with turns_file.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("prompt_id"):
                    ids.add(str(row["prompt_id"]))
    except OSError:
        pass
    return ids


def _read_new_lines(transcript: Path, offset: int) -> tuple[list[dict], int, bool]:
    """Records after *offset*, the new offset, and whether the cursor was reset.

    Only complete lines are consumed; a partial trailing line waits for the
    next call. If the file is shorter than the offset (pruned or rewritten),
    the walk restarts from zero.
    """
    size = transcript.stat().st_size
    reset = False
    if offset > size or offset < 0:
        offset, reset = 0, True
    with transcript.open("rb") as f:
        f.seek(offset)
        data = f.read()
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], offset, reset
    complete = data[: cut + 1]
    records: list[dict] = []
    for raw in complete.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, offset + len(complete), reset


def update_from_transcript(
    transcript_path: str | Path, session_id: str, *, start: Path | None = None
) -> list[dict]:
    """Append the pairs the transcript gained since the last call. Returns them.

    Safe to call at every Stop: the cursor makes the work proportional to what
    the turn appended, and ``prompt_id`` de-duplication makes a re-walk (cursor
    reset) idempotent. Raises ``OSError`` if the transcript cannot be read —
    the dispatcher logs it and the chain continues.
    """
    transcript = Path(transcript_path)
    if not transcript.is_file():
        return []
    turns_file = store.turns_path(session_id, start)
    cursor_file = store.cursor_path(session_id, start)
    turns_file.parent.mkdir(parents=True, exist_ok=True)

    with FileLock(cursor_file):
        cursor = _load_cursor(cursor_file)
        offset = cursor.get("offset", 0) if isinstance(cursor.get("offset"), int) else 0
        records, new_offset, reset = _read_new_lines(transcript, offset)
        state = PairState(
            carry=None if reset else cursor.get("carry"),
            carry_ts=None if reset else cursor.get("carry_ts"),
            seen_prompt_ids=_existing_prompt_ids(turns_file) if reset else set(),
        )
        if not records and not reset:
            return []
        pairs = extract_pairs(records, state)
        if pairs:
            with turns_file.open("a", encoding="utf-8") as f:
                for pair in pairs:
                    f.write(json.dumps(pair, ensure_ascii=False) + "\n")
        write_json_atomic(
            cursor_file,
            {
                "offset": new_offset,
                "carry": state.carry,
                "carry_ts": state.carry_ts,
                "transcript": str(transcript),
            },
        )
    return pairs


def load_pairs(session_id: str, *, start: Path | None = None) -> list[dict]:
    """Every pair logged for a session, in transcript order."""
    path = store.turns_path(session_id, start)
    pairs: list[dict] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    pairs.append(row)
    except OSError:
        return []
    return pairs


def list_turn_sessions(start: Path | None = None) -> list[str]:
    """Short session ids that have a turns file, oldest first."""
    folder = store.turns_dir(start)
    if not folder.is_dir():
        return []
    files = [p for p in folder.glob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime)
    return [p.stem for p in files]
