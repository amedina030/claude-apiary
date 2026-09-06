"""Cheap output heuristics over Claude's final message of each turn (D-2026-62).

The rule table's primary signal is the user's own corrections and acceptances
(``compass/classify.py``). For the **output** rules there is also a model-free
secondary signal the Stop hook can compute on every turn: does the final
message of the turn lead with an outcome, does it carry at most one
recommendation, and is it inside a length band. These are crude by design —
regexes over free text, no model call — and they are **never** counted in a
row's confidence: ``compass/rules.py build`` summarises them under the Output
section as rates, and that is all they are for.

Written by ``compass/turns.py`` (from the Stop hook) to
``<state-dir>/compass/events/<sid>.heuristics.jsonl``: one JSON object per
finished assistant turn, ``source: "heuristic"``. The ``.heuristics.jsonl``
suffix keeps them out of ``rules.load_events``'s ``*.json`` glob.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from compass import store

#: The final message should be neither a one-liner nor an essay.
LENGTH_BAND = (150, 3000)
#: A first sentence longer than this is not "the outcome" but a paragraph.
FIRST_SENTENCE_MAX_WORDS = 40

#: Process narration a first sentence must not start with to count as an
#: outcome. Lowercase; matched after markdown markers are stripped.
_PROCESS_OPENERS = (
    "i'll ",
    "i will ",
    "i'm going to ",
    "i am going to ",
    "i've started",
    "let me ",
    "let's ",
    "first,",
    "first ",
    "now ",
    "next,",
    "next ",
    "looking ",
    "reading ",
    "starting ",
    "checking ",
    "running ",
    "going to ",
    "before ",
    "to ",
    "sure",
    "okay",
    "ok,",
    "ok ",
    "great",
    "here's ",
    "here is ",
)

_RECOMMENDATION_RE = re.compile(
    r"\b(i recommend|my recommendation|recommended (?:option|approach|path|fix)|"
    r"i'd go with|i would go with|i suggest|go with|best option|recommendation:)",
    re.IGNORECASE,
)
_MENU_RE = re.compile(r"\b(option [a-d1-4]\b|alternatively\b)", re.IGNORECASE)
_MARKDOWN_LEAD_RE = re.compile(r"^[\s#>*_`\-]+")
_EMPHASIS_RE = re.compile(r"[*_`]+")
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")

SOURCE = "heuristic"


def first_sentence(text: str) -> str:
    """The first sentence of *text* with leading markdown markers removed."""
    stripped = text.strip()
    if not stripped:
        return ""
    para = stripped.split("\n\n", 1)[0]
    para = _EMPHASIS_RE.sub("", _MARKDOWN_LEAD_RE.sub("", para)).strip()
    m = _SENTENCE_END_RE.search(para)
    sentence = para[: m.end()] if m else para
    return " ".join(sentence.split())


def outcome_first(text: str) -> bool:
    """Does the first sentence read as an outcome rather than narration?

    Not a question, not a process opener (``I'll``, ``Let me``, ``Now``…),
    and short enough to be a sentence rather than a paragraph.
    """
    sentence = first_sentence(text)
    if not sentence or sentence.endswith("?"):
        return False
    lowered = sentence.lower()
    if lowered.startswith(_PROCESS_OPENERS):
        return False
    return len(sentence.split()) <= FIRST_SENTENCE_MAX_WORDS


def one_recommendation(text: str) -> bool:
    """At most one recommendation and no menu of alternatives."""
    return len(_RECOMMENDATION_RE.findall(text)) <= 1 and len(_MENU_RE.findall(text)) <= 1


def length_band(text: str) -> bool:
    low, high = LENGTH_BAND
    return low <= len(text.strip()) <= high


def score_output(text: str) -> dict:
    """The three booleans plus the character count for one final message."""
    return {
        "chars": len(text.strip()),
        "outcome_first": outcome_first(text),
        "one_recommendation": one_recommendation(text),
        "length_band": length_band(text),
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def record_turn(session_id: str, text: str, ts: str | None, *, start: Path | None = None) -> dict:
    """Score *text* and append one line to the session's heuristics file."""
    row = {"ts": ts, "source": SOURCE, **score_output(text)}
    path = store.heuristics_path(session_id, start)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return row


def load_session(session_id: str, *, start: Path | None = None) -> list[dict]:
    """Every heuristic row logged for one session, in order."""
    path = store.heuristics_path(session_id, start)
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append({**row, "session_id": store.short_session_id(session_id)})
    except OSError:
        return []
    return rows


def load_classified(folder: Path | None = None) -> list[dict]:
    """Heuristic rows of every session that has an events file.

    Tied to classification on purpose: a live session appends a row at every
    Stop, and the rules table should only move when a session is classified.
    """
    folder = store.events_dir() if folder is None else Path(folder)
    if not folder.is_dir():
        return []
    rows: list[dict] = []
    for events_file in sorted(folder.glob("*.json")):
        sid = events_file.stem
        path = folder / f"{sid}{store.HEURISTICS_SUFFIX}"
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append({**row, "session_id": sid})
        except OSError:
            continue
    return rows


def summarize(rows: list[dict]) -> dict:
    """Counts for the summary line: turns, sessions and each heuristic's hits."""
    summary = {
        "turns": 0,
        "sessions": 0,
        "outcome_first": 0,
        "one_recommendation": 0,
        "length_band": 0,
    }
    sessions: set[str] = set()
    for row in rows:
        summary["turns"] += 1
        sessions.add(str(row.get("session_id")))
        for key in ("outcome_first", "one_recommendation", "length_band"):
            if row.get(key) is True:
                summary[key] += 1
    summary["sessions"] = len(sessions)
    return summary
