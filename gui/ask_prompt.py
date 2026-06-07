"""Detect pending AskUserQuestion prompts from the JSONL transcript.

The GUI used to scrape AskUserQuestion option cards out of the live xterm
buffer. That never worked reliably: the card renders its option numbers flush
at column 0 (the scrape parser rejected them) and, worse, a card taller than the
pty viewport only ever has the scrolled-into-view subset of options in the
buffer at all — so the banner showed partial menus or none. See L-2026-133.

The structured truth already exists upstream: AskUserQuestion is a tool call, so
the assistant record in the transcript carries the complete, exact option list
as a ``tool_use`` block, and its answer arrives as a matching ``tool_result``.
This watcher observes those raw records (via TranscriptTail.on_record) and emits
a prompt payload when one is pending and a resolved signal when it's answered —
no terminal scraping involved.

Pure and side-effect-free apart from the two injected callbacks, so it unit-
tests against real captured record shapes without a GUI or a pty.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

ASK_TOOL_NAME = "AskUserQuestion"


def _content_blocks(rec: dict) -> list:
    """The ``message.content`` list of a record, or [] for non-list content.

    User text records carry a string content (not a list); only assistant
    turns and tool_result envelopes use list-form content, which is where the
    tool_use / tool_result blocks live.
    """
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    return content if isinstance(content, list) else []


def extract_ask_prompt(rec: dict) -> Optional[dict]:
    """If ``rec`` is an assistant record containing an AskUserQuestion tool_use,
    return ``{"tool_use_id": str, "questions": list}``; otherwise None.

    Only the questions list is forwarded — the frontend owns banner shaping.
    """
    if rec.get("type") != "assistant":
        return None
    for blk in _content_blocks(rec):
        if (
            isinstance(blk, dict)
            and blk.get("type") == "tool_use"
            and blk.get("name") == ASK_TOOL_NAME
        ):
            inp = blk.get("input")
            questions = inp.get("questions") if isinstance(inp, dict) else None
            if isinstance(questions, list) and questions:
                return {"tool_use_id": blk.get("id", ""), "questions": questions}
    return None


def extract_resolved_ids(rec: dict) -> list:
    """tool_use_ids answered by any tool_result block in ``rec``.

    A tool_result lands in a user record whether the prompt was answered or
    cancelled/rejected — either way the prompt is no longer pending, so the
    banner should clear.
    """
    ids = []
    for blk in _content_blocks(rec):
        if isinstance(blk, dict) and blk.get("type") == "tool_result":
            tid = blk.get("tool_use_id")
            if tid:
                ids.append(tid)
    return ids


class AskPromptWatcher:
    """Stateful observer fed every raw transcript record.

    Tracks which AskUserQuestion prompts are still unanswered and calls
    ``on_prompt(payload)`` exactly once when one first appears, then
    ``on_resolved(tool_use_id)`` once when its tool_result arrives. Both
    callbacks are wrapped so a frontend-push failure can't crash the tail
    thread.
    """

    def __init__(
        self,
        on_prompt: Callable[[dict], None],
        on_resolved: Callable[[str], None],
    ) -> None:
        self._on_prompt = on_prompt
        self._on_resolved = on_resolved
        # tool_use_id -> payload, for prompts seen but not yet resolved.
        self._pending: dict[str, dict] = {}
        self._emit = True

    def reset(self) -> None:
        """Drop all pending state — called when the tail re-attaches to a fresh
        transcript (e.g. after /clear) so a prior session's prompt can't linger.
        """
        self._pending.clear()

    def note_record(self, rec: dict) -> None:
        if not isinstance(rec, dict):
            return
        prompt = extract_ask_prompt(rec)
        if prompt is not None:
            tid = prompt["tool_use_id"]
            if tid and tid not in self._pending:
                self._pending[tid] = prompt
                if self._emit:
                    self._safe(self._on_prompt, prompt)
            return
        for tid in extract_resolved_ids(rec):
            if self._pending.pop(tid, None) is not None and self._emit:
                self._safe(self._on_resolved, tid)

    def replay(self, records: Iterable[dict]) -> None:
        """Process history silently, then surface only prompts still unanswered.

        At attach time the transcript may already contain answered prompts;
        replaying them with callbacks live would flash-then-hide a stale banner.
        So we fold history into ``_pending`` with emission off, then fire
        ``on_prompt`` once for each prompt that's genuinely still waiting.
        """
        self._emit = False
        try:
            for rec in records:
                self.note_record(rec)
        finally:
            self._emit = True
        for payload in list(self._pending.values()):
            self._safe(self._on_prompt, payload)

    @staticmethod
    def _safe(fn: Callable, arg) -> None:
        try:
            fn(arg)
        except Exception:
            pass
