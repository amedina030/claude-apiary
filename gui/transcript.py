"""Transcript parsing, message filtering, token extraction, and live tail.

The JSONL transcript is the sole source of truth for rendered conversation. We never
parse pty stdout for messages.

Filter rules (from spec C-2026-32 + JSONL inspection):
- Drop records with `type` != "user"/"assistant".
- Drop any record with `isMeta == true`. Claude Code sets this on synthetic
  user-role records that the user did not author -- slash-command caveats,
  loaded skill bodies, and (the motivating case for this filter) ScheduleWakeup
  callbacks that the model itself queued. Without this guard, those land in
  the chat as user bubbles even though the user never typed them.
- Keep `type=="user"` iff: `promptId` present, `attachment` absent, `message.role=="user"`,
  and `message.content` is a non-empty string. (List-form content on a user record is
  always a tool_result envelope — drop it.)
- Keep `type=="assistant"` and emit only the text-typed blocks from `message.content`.
  Drop tool_use blocks. Read `message.usage` for token counts.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write

    def to_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }


@dataclass
class Message:
    role: str  # "user" | "assistant"
    text: str
    timestamp: str  # ISO-8601 string from the JSONL
    uuid: str
    tokens: Optional[TokenUsage] = None
    model: str = ""  # only set on assistant records

    def to_dict(self) -> dict:
        d = {
            "role": self.role,
            "text": self.text,
            "timestamp": self.timestamp,
            "uuid": self.uuid,
        }
        if self.tokens is not None:
            d["tokens"] = self.tokens.to_dict()
        if self.model:
            d["model"] = self.model
        return d


def _extract_usage(usage_obj: dict) -> TokenUsage:
    if not isinstance(usage_obj, dict):
        return TokenUsage()
    return TokenUsage(
        input=int(usage_obj.get("input_tokens", 0) or 0),
        output=int(usage_obj.get("output_tokens", 0) or 0),
        cache_read=int(usage_obj.get("cache_read_input_tokens", 0) or 0),
        cache_write=int(usage_obj.get("cache_creation_input_tokens", 0) or 0),
    )


def filter_record(rec: dict) -> Optional[Message]:
    """Return a Message if `rec` should be rendered, else None."""
    if not isinstance(rec, dict):
        return None
    # `isMeta` flags synthetic/system-injected records (slash commands, loaded
    # skill bodies, ScheduleWakeup callbacks). The user did not author them, so
    # they should never appear as a user bubble in the chat.
    if rec.get("isMeta") is True:
        return None
    rtype = rec.get("type")
    msg = rec.get("message")
    uuid = rec.get("uuid", "")
    ts = rec.get("timestamp", "")

    if rtype == "user":
        if "attachment" in rec or "promptId" not in rec:
            return None
        if not isinstance(msg, dict) or msg.get("role") != "user":
            return None
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        # Claude Code wraps slash-command invocations in a synthetic user record
        # that starts with <local-command-caveat>…</local-command-caveat> plus
        # <command-name>/foo</command-name>. It's not user-authored prose — it's
        # an internal marker — so drop it from the rendered chat. Same deal for
        # <task-notification> (harness-injected on background-bash completion).
        stripped = content.lstrip()
        if (
            stripped.startswith("<local-command-caveat>")
            or stripped.startswith("<command-name>")
            or stripped.startswith("<task-notification>")
        ):
            return None
        return Message(role="user", text=content, timestamp=ts, uuid=uuid)

    if rtype == "assistant":
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            return None
        content = msg.get("content")
        if not isinstance(content, list):
            return None
        text_parts = [
            blk.get("text", "")
            for blk in content
            if isinstance(blk, dict) and blk.get("type") == "text"
        ]
        text = "".join(text_parts).strip()
        if not text:
            return None
        tokens = _extract_usage(msg.get("usage", {}))
        model = msg.get("model", "") if isinstance(msg.get("model", ""), str) else ""
        return Message(
            role="assistant",
            text=text,
            timestamp=ts,
            uuid=uuid,
            tokens=tokens,
            model=model,
        )

    return None


def parse_jsonl_lines(text: str) -> list[Message]:
    """Replay helper: parse a chunk of JSONL text into rendered messages.

    Malformed lines are silently skipped; the count is returned via skipped_count
    if you re-implement with state. Used at startup to replay history.
    """
    out: list[Message] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = filter_record(rec)
        if msg is not None:
            out.append(msg)
    return out


class TranscriptTail:
    """Replay-then-tail a single JSONL transcript file.

    Polls on a short interval (default 500ms) instead of using watchdog so the
    behavior stays portable and dependency-light. Watchdog is wired in by the
    higher-level service for instant file-event triggers; this class is the
    pure read loop.
    """

    def __init__(
        self,
        path: Path,
        on_message: Callable[[Message], None],
        on_skip: Optional[Callable[[int], None]] = None,
        poll_interval: float = 0.5,
    ) -> None:
        self.path = path
        self.on_message = on_message
        self.on_skip = on_skip or (lambda _n: None)
        self.poll_interval = poll_interval
        self._pos = 0
        self._buf = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._skipped = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="transcript-tail", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def poke(self) -> None:
        """Wake the loop early — call from a watchdog file-event handler."""
        # Polling is short enough that an extra signal doesn't help unless we add
        # a Condition. Kept as a no-op stub for the watchdog wiring.
        return

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._read_new_bytes()
            except FileNotFoundError:
                pass
            except Exception:
                # Don't crash the tail thread; surface skip counter on next event.
                self._skipped += 1
                self.on_skip(self._skipped)
            self._stop.wait(self.poll_interval)

    def _read_new_bytes(self) -> None:
        with self.path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(self._pos)
            chunk = f.read()
            self._pos = f.tell()
        if not chunk:
            return
        self._buf += chunk
        # Split on newline, keep last partial line in buffer.
        lines = self._buf.split("\n")
        self._buf = lines[-1]
        for line in lines[:-1]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                self._skipped += 1
                self.on_skip(self._skipped)
                continue
            msg = filter_record(rec)
            if msg is not None:
                self.on_message(msg)


def find_active_session_jsonl(
    projects_root: Optional[Path] = None,
    min_ctime: float = 0.0,
    exclude_paths: Optional[set[Path]] = None,
) -> Optional[Path]:
    """Return the most recently modified `*.jsonl` under ~/.claude/projects/, or None.

    `min_ctime` filters out files created before that timestamp (anchor to "since
    GUI spawned its claude subprocess"). `exclude_paths` filters out specific
    JSONLs known to belong to external sessions. Together they pin the GUI to
    its own spawned subprocess and ignore other claudes the user runs.
    """
    root = projects_root or (Path.home() / ".claude" / "projects")
    if not root.is_dir():
        return None
    excluded = exclude_paths or set()
    candidates: list[tuple[float, Path]] = []
    for p in root.rglob("*.jsonl"):
        if p in excluded:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_ctime < min_ctime:
            continue
        candidates.append((st.st_mtime, p))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def snapshot_existing_jsonls(projects_root: Optional[Path] = None) -> set[Path]:
    """Return the set of `*.jsonl` paths currently present under projects_root.

    Used by the GUI to capture pre-spawn JSONLs so they're excluded from
    session-discovery once the spawned claude creates its own transcript.
    """
    root = projects_root or (Path.home() / ".claude" / "projects")
    if not root.is_dir():
        return set()
    out: set[Path] = set()
    for p in root.rglob("*.jsonl"):
        out.add(p)
    return out


# Expose a session-watch loop helper that callers can spawn alongside TranscriptTail
# to detect /clear (new JSONL file appearing).
class SessionDiscovery:
    def __init__(
        self,
        on_switch: Callable[[Path], None],
        projects_root: Optional[Path] = None,
        poll_interval: float = 2.0,
        min_ctime: float = 0.0,
        exclude_paths: Optional[set[Path]] = None,
        lock_after_first: bool = False,
    ) -> None:
        self.on_switch = on_switch
        self.projects_root = projects_root or (Path.home() / ".claude" / "projects")
        self.poll_interval = poll_interval
        self._current: Optional[Path] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._min_ctime = min_ctime
        self._exclude_paths: set[Path] = set(exclude_paths or ())
        # Once we successfully lock onto a JSONL, ignore later files. Prevents
        # subagent / Task-tool sidechain JSONLs (created post-spawn but belonging
        # to a different conversation) from stealing focus.
        self._lock_after_first = lock_after_first

    def set_pin(
        self,
        min_ctime: float,
        exclude_paths: Optional[set[Path]] = None,
        lock_after_first: Optional[bool] = None,
    ) -> None:
        """Lock discovery to JSONLs created at/after min_ctime, excluding the given paths.

        Called after the GUI spawns its claude subprocess so we ignore the parent
        terminal's session and any other pre-existing claude sessions.
        """
        with self._lock:
            self._min_ctime = min_ctime
            if exclude_paths is not None:
                self._exclude_paths = set(exclude_paths)
            if lock_after_first is not None:
                self._lock_after_first = lock_after_first
            # Drop the current pick if it no longer satisfies the pin.
            if self._current is not None and (
                self._current in self._exclude_paths
                or (self._current.exists() and self._current.stat().st_ctime < min_ctime)
            ):
                self._current = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="session-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                min_ctime = self._min_ctime
                exclude = set(self._exclude_paths)
                locked = self._lock_after_first and self._current is not None
            if locked:
                # We've already locked onto a JSONL; don't switch unless it goes away.
                if self._current is None or not self._current.exists():
                    with self._lock:
                        self._current = None
                self._stop.wait(self.poll_interval)
                continue
            try:
                latest = find_active_session_jsonl(
                    self.projects_root,
                    min_ctime=min_ctime,
                    exclude_paths=exclude,
                )
            except Exception:
                latest = None
            if latest is not None and latest != self._current:
                self._current = latest
                self.on_switch(latest)
            self._stop.wait(self.poll_interval)
