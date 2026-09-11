"""The wire format between the two sessions: a preamble out, a reply back.

There is no far-side skill file and no mailbox. The preamble *is* the protocol
and the ``claude -p`` JSON envelope is the transport, so a callee needs nothing
installed to take a call beyond the apiary chain it already has.

The preamble tells the callee four things: who is asking, what it may do, how
to shape the reply, and that it must not place a call of its own. The reply
comes back as three headed sections, and :func:`parse_reply` splits them so the
caller's session receives an answer, any questions aimed back at it, and the
list of changes an act call made.
"""

from __future__ import annotations

import re

#: Section headings the callee is asked to use, in order.
ANSWER_HEADING = "Answer"
QUESTIONS_HEADING = "Questions for the caller"
CHANGES_HEADING = "Changes made"
SECTIONS = (ANSWER_HEADING, QUESTIONS_HEADING, CHANGES_HEADING)

#: Nothing here needs a heading level deeper than two.
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*#*\s*$", re.MULTILINE)

REPLY_FORMAT = "\n".join(
    [
        "Shape the whole reply like this, with these three headings and nothing above them:",
        "",
        f"## {ANSWER_HEADING}",
        "What the caller asked for. Prose, as short as the question allows.",
        "",
        f"## {QUESTIONS_HEADING}",
        "Anything you need from the caller to finish, one per line. Write `none` if there is nothing.",
        "",
        f"## {CHANGES_HEADING}",
        "Files or state you changed, one per line. Write `none` if you changed nothing.",
    ]
)

ANSWER_RULES = (
    "This is an answer-mode call. Read whatever you need and answer the question. "
    "Do not edit, create or delete any file, do not commit, and do not run anything "
    "that changes this repo's state."
)

ACT_RULES = (
    "This is an act-mode call. You may change files in this checkout. Before your "
    "first edit, create and switch to the branch named below, and leave the checkout "
    "back on the branch it started on when you are done. Never push, never open or "
    "merge a pull request, and never touch a remote."
)

NO_NESTED_CALLS = (
    "Do not place a telephone call of your own. You are already inside one, and a "
    "nested call multiplies cost and can loop. If answering needs a third repo, say "
    "so under the questions heading and let the caller decide."
)


def build_preamble(
    *,
    caller: str,
    callee: str,
    mode: str,
    call_id: str,
    message: str,
    branch: str | None = None,
    prior_exchanges: list[dict] | None = None,
    initiated_by: str = "model",
) -> str:
    """The full prompt handed to the callee run on stdin.

    *prior_exchanges* is quoted back only when a follow-up could not resume the
    callee's own session; a resumed run already remembers them.
    """
    who = "the user" if initiated_by == "user" else f"a Claude session working in {caller}"
    lines = [
        f"You are taking a telephone call from {caller}, another repository managed by the "
        "same apiary toolkit. Your working directory is the repo being called, and its "
        "CLAUDE.md, hooks and notes apply to you as usual.",
        "",
        f"Call id: {call_id}",
        f"Caller: {caller}",
        f"Callee (you): {callee}",
        f"Mode: {mode}",
        f"Placed by: {who}",
    ]
    if branch:
        lines.append(f"Work branch: {branch}")
    lines.extend(["", ACT_RULES if mode == "act" else ANSWER_RULES, "", NO_NESTED_CALLS, ""])

    if prior_exchanges:
        lines.append(
            "This line has already been open. The earlier turns are quoted below "
            "because your previous session could not be resumed."
        )
        lines.append("")
        for item in prior_exchanges:
            lines.append(f'<exchange number="{item.get("number", "?")}">')
            lines.append("caller said:")
            lines.append((item.get("message") or "").strip())
            lines.append("you replied:")
            lines.append((item.get("reply") or "").strip())
            lines.append("</exchange>")
            lines.append("")

    lines.extend(["The caller says:", "", "<message>", message.strip(), "</message>", ""])
    lines.append(REPLY_FORMAT)
    return "\n".join(lines) + "\n"


def _split_sections(text: str) -> dict[str, str]:
    """Map every heading in *text* to the body under it, lowercased keys."""
    out: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(text or ""))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[match.group(1).strip().lower()] = text[match.end() : end].strip()
    return out


def _as_items(block: str) -> list[str]:
    """Lines of a list section, with bullets and a lone ``none`` stripped."""
    items: list[str] = []
    for raw in (block or "").splitlines():
        line = raw.strip().lstrip("-*").strip()
        if not line:
            continue
        if line.lower().rstrip(".") in ("none", "n/a", "nothing"):
            continue
        items.append(line)
    return items


def parse_reply(text: str) -> dict:
    """Split a callee reply into ``answer``, ``questions`` and ``changes``.

    A reply that ignored the format is not an error: the whole text becomes the
    answer and the two lists come back empty, so a callee that answered in
    plain prose still reaches the caller intact.
    """
    body = (text or "").strip()
    sections = _split_sections(body)
    answer = sections.get(ANSWER_HEADING.lower())
    if answer is None:
        preamble = body
        first = _HEADING_RE.search(body)
        if first is not None:
            preamble = body[: first.start()].strip()
        return {
            "answer": preamble or body,
            "questions": [],
            "changes": [],
            "structured": False,
        }
    return {
        "answer": answer,
        "questions": _as_items(sections.get(QUESTIONS_HEADING.lower(), "")),
        "changes": _as_items(sections.get(CHANGES_HEADING.lower(), "")),
        "structured": True,
    }


def format_reply(parsed: dict, *, call_id: str, mode: str, initiated_by: str, status: str) -> str:
    """What the caller's session sees on stdout.

    Deliberately small. The caller's context is the scarce resource this whole
    tool exists to protect, so the reply text, the two short lists and a status
    line are all that come back.
    """
    lines = [parsed.get("answer") or "(no answer)"]
    questions = parsed.get("questions") or []
    if questions:
        lines.append("")
        lines.append(f"{QUESTIONS_HEADING}:")
        lines.extend(f"- {q}" for q in questions)
    changes = parsed.get("changes") or []
    if changes:
        lines.append("")
        lines.append(f"{CHANGES_HEADING}:")
        lines.extend(f"- {c}" for c in changes)
    lines.append("")
    lines.append(f"[telephone] {call_id} mode={mode} initiated_by={initiated_by} status={status}")
    return "\n".join(lines)
