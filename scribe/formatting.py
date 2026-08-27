"""Display helpers for scribe: IDs, ages, colour, and the list renderers.

Separate from ``notes.py`` because these are not CLI logic — ``core/startup``
renders the session banner with ``format_age`` and ``format_id``, and used to
import them from a CLI module to get them (review, knowledge.md).
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.utils.timeutil import parse_iso
from scribe.store import TYPE_PREFIXES

#: Display-ID prefix → note type. The inverse of ``TYPE_PREFIXES``.
PREFIX_TO_TYPE: dict[str, str] = {v: k for k, v in TYPE_PREFIXES.items()}

_ID_RE = re.compile(r"^([A-Z])-([0-9]{4})-([0-9]+)$")

_ANSI_RESET = "\x1b[0m"
_STATUS_TAG_COLORS = {"[DONE]": "32", "[DROPPED]": "31", "[DEFERRED]": "33", "[ARCHIVED]": "35"}

#: Statuses that get a bracketed tag in a list row.
_TAGGED_STATUSES = ("done", "dropped", "deferred")


# --------------------------------------------------------------------------- #
# IDs
# --------------------------------------------------------------------------- #


def format_id(entry: dict) -> str:
    """The ``TYPE-YEAR-seq`` display ID for a note or learning row."""
    prefix = TYPE_PREFIXES.get(entry.get("type", ""), "?")
    return f"{prefix}-{entry.get('year', '?')}-{entry.get('seq', '?')}"


def parse_id(raw: str) -> "tuple | None":
    """Parse ``T-2026-1`` → ``('todo', 2026, 1)``. None when it isn't one.

    Case-insensitive on the prefix. The only accepted ID form — the legacy
    bare integers went out with the migration map.
    """
    match = _ID_RE.match((raw or "").strip().upper())
    if not match:
        return None
    note_type = PREFIX_TO_TYPE.get(match.group(1))
    if note_type is None:
        return None
    return (note_type, int(match.group(2)), int(match.group(3)))


# --------------------------------------------------------------------------- #
# Ages
# --------------------------------------------------------------------------- #


def format_age(ts) -> str:
    """A relative age string ('5m ago', '3d ago') from an ISO timestamp."""
    dt = parse_iso(ts)
    if dt is None:
        return "unknown"
    total_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if total_seconds < 0:
        return "in the future"
    minutes = int(total_seconds / 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        return f"{days // 7}w ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #


def color_enabled() -> bool:
    """True when colour is requested: FORCE_COLOR set and NO_COLOR unset.

    Deliberately never gates on ``stdout.isatty()``: this tool runs inside the
    GUI (a non-TTY pipe) where isatty misreports, and raw ANSI escapes would
    render as literal garbage in the chat pane (spec §5.13, decision #4).
    """
    return bool(os.environ.get("FORCE_COLOR")) and not os.environ.get("NO_COLOR")


def colorize(text: str, *codes: str) -> str:
    """Wrap *text* in ANSI SGR *codes* when colour is on, else return it as-is."""
    if not codes or not color_enabled():
        return text
    return f"\x1b[{';'.join(codes)}m{text}{_ANSI_RESET}"


def _status_tag(status: str) -> str:
    """The ' [DONE]'-style suffix for a row's status; '' for a live note."""
    if status not in _TAGGED_STATUSES:
        return ""
    label = f"[{status.upper()}]"
    code = _STATUS_TAG_COLORS.get(label)
    return f" {colorize(label, code)}" if code else f" {label}"


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


def note_line(entry: dict) -> str:
    """One row of ``notes.py list``: ID, type, age, summary, status tag."""
    note_type = entry.get("type", "?")[:8]
    age = format_age(entry.get("timestamp", ""))
    summary = entry.get("summary", "").replace("\n", " ")[:80]
    tag = _status_tag(entry.get("status", ""))
    display_id = format_id(entry)
    if color_enabled():
        return (
            f"{colorize(f'{display_id:<12}', '1', '36')} "
            f"{colorize(f'{note_type:<10}', '90')} "
            f"{colorize(f'({age:<9})', '2')} {summary}{tag}"
        )
    # ASCII-folded: this output is read back through pipes with unknown
    # encodings (the GUI, a hook's captured stdout) where a stray em-dash
    # from a note body used to raise UnicodeEncodeError.
    line = f"{display_id:<12} {note_type:<10} ({age:<9}) {summary}{tag}"
    return line.encode("ascii", errors="replace").decode("ascii")


def note_detail(note: dict, *, is_learning: bool = False) -> list:
    """The full-note view ``notes.py get`` prints, as lines.

    Role, mission and the auto-generated flag appear only when they say
    something: a learning has no status and no auto flag, and a note written
    before identities existed has neither role nor mission.
    """
    lines = [f"ID: {format_id(note)}"]
    if is_learning:
        lines.append("Type: learning")
    else:
        lines.append(f"Type: {note.get('type', '?')}")
        lines.append(f"Status: {note.get('status', '?')}")
    lines.append(f"Session: {note.get('session', '?')}")
    stamp = note.get("timestamp", "")
    lines.append(f"Time: {note.get('timestamp', '?')} ({format_age(stamp)})")
    for field in ("role", "mission"):
        if note.get(field):
            lines.append(f"{field.capitalize()}: {note[field]}")
    if not is_learning:
        lines.append(f"Auto: {note.get('auto_generated', False)}")
    lines.append("---")
    lines.append(note.get("content", ""))
    return lines


def learning_line(entry: dict) -> str:
    """One row of ``notes.py learnings``: ID, age, truncated summary."""
    summary = entry.get("summary", "").replace("\n", " ")[:80]
    return f"{format_id(entry):<12} ({format_age(entry.get('timestamp', '')):<9}) {summary}"


def learnings_index(learnings: list) -> list:
    """The tag-grouped compact index the startup hook injects.

    Primary tag = the first entry in ``tags``; untagged rows land in their own
    bucket, listed last. Named groups are alphabetical, so the injected block
    is byte-stable between sessions that changed nothing.
    """
    groups: dict[str, list] = {}
    for entry in learnings:
        tags = entry.get("tags") or []
        groups.setdefault(tags[0] if tags else "untagged", []).append(entry)

    named = sorted(k for k in groups if k != "untagged")
    lines: list = []
    for tag in named + (["untagged"] if "untagged" in groups else []):
        items = groups[tag]
        lines.append(f"[{tag}] ({len(items)})")
        for entry in items:
            summary = entry.get("summary", "").replace("\n", " ")[:80]
            lines.append(f"  {format_id(entry):<12} {summary}")
    return lines
