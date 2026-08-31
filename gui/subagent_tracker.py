"""Track subagent JSONL files for the active session.

Polls ``<parent_jsonl_dir>/<parent_session_id>/subagents/agent-*.jsonl`` on
a fixed cadence, combines per-file metadata from the ``.meta.json`` sidecar
written by Claude Code when an Agent is spawned, and emits a full list of
``AgentState`` snapshots to a callback. The frontend panel renders from
those snapshots.

Running vs done:
- ``running`` while the JSONL grows and no assistant message has arrived
  with ``stop_reason == "end_turn"``.
- ``done`` once an ``end_turn`` assistant message is observed (subagents
  emit one terminal message at the end of their run).

Tokens, current tool, and tool histogram are derived by tailing the
subagent JSONL incrementally — same byte-offset pattern as
``TranscriptTail``. Files that vanish (unusual, but possible on /clear)
are dropped from state.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# If a subagent's final assistant record ends with a text block but no
# `stop_reason` field (intermittent Claude Code behavior — observed in
# practice), we fall back to marking it done once this many seconds have
# passed with no further JSONL growth.
IDLE_DONE_TIMEOUT_S = 10.0

# Safety net for the case where neither the primary signal (parent's
# Agent tool_result — see note_parent_record) nor the ends-in-text fast
# path fires. Covers e.g. a manually-deleted parent JSONL or a runaway
# process whose parent never wrote the tool_result. Generous so it
# doesn't false-positive long legitimate tool calls with no intermediate
# writes. A subagent that resumes writing flips back to running.
STALE_DONE_TIMEOUT_S = 120.0


@dataclass
class TokenTotals:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class AgentState:
    agent_id: str
    subagent_type: str
    description: str
    model: str
    prompt_preview: str
    started_at: float  # epoch seconds
    last_activity_at: float  # epoch seconds
    current_tool: str
    tokens: TokenTotals
    tool_histogram: dict[str, int] = field(default_factory=dict)
    final_text: str = ""
    status: str = "running"  # "running" | "done"

    def to_dict(self) -> dict:
        # asdict recurses into TokenTotals, so tokens arrives as
        # {"input", "output", "cache_read", "cache_write"} — already the keys
        # app.js reads. No renaming happens, and none may: the change-detection
        # hash and the onAgents payload both depend on this dict as-is.
        return asdict(self)


def _parse_ts(ts: Optional[str]) -> Optional[float]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class SubagentTracker:
    """Polls one parent session's ``subagents/`` dir at ``poll_interval``
    seconds. Calls ``on_update(list[AgentState], parent_session_id)`` when
    the aggregated snapshot changes. Starts a daemon thread; ``stop()`` to
    shut it down.

    ``session_jsonl_fn`` returns the currently-active parent JSONL ``Path``
    for the session, or ``None`` if nothing is active yet. ``session_id_fn``
    returns the GUI session id string (routed to the correct tab on push).
    """

    def __init__(
        self,
        session_jsonl_fn: Callable[[], Optional[Path]],
        session_id_fn: Callable[[], str],
        on_update: Callable[[list[AgentState], str], None],
        poll_interval: float = 2.0,
    ) -> None:
        self._session_jsonl_fn = session_jsonl_fn
        self._session_id_fn = session_id_fn
        self._on_update = on_update
        self._poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_parent: Optional[Path] = None
        # {agent_id: {"pos": int, "buf": str, "state": AgentState, "ends_in_text": bool,
        #             "resolved_by_parent": bool}}
        self._agents: dict[str, dict] = {}
        self._last_snapshot_key: Optional[str] = None
        # Serialises access to _agents and _parent_agent_calls. The scan loop
        # owns _agents; note_parent_record is called from the TranscriptTail
        # thread and mutates the same dict when it resolves an Agent call.
        self._lock = threading.RLock()
        # Parent Agent tool_use records keyed by tool_use_id. Populated when
        # the parent's assistant message spawns an Agent and cleared when the
        # corresponding tool_result arrives. Entry shape:
        #   {"description": str, "prompt": str, "spawn_ts": float, "resolved": bool}
        # Bounded in practice by the count of Agent spawns per session.
        self._parent_agent_calls: dict[str, dict] = {}
        # tool_result resolutions that arrived before the matching subagent
        # JSONL appeared in _agents. Drained on each scan_once. Entries:
        #   {"description": str, "parent_spawn_ts": float, "seen_ts": float}
        self._pending_resolutions: list[dict] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="subagent-tracker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3.0)

    # --- main loop -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as e:  # noqa: BLE001 — keep the poller alive
                print(f"[subagent-tracker] scan error: {e}", file=sys.stderr)
            self._stop.wait(self._poll_interval)

    def _scan_once(self) -> None:
        parent = self._session_jsonl_fn()
        with self._lock:
            # Parent switched (e.g. /clear) → drop prior state but keep scanning.
            if parent != self._last_parent:
                self._last_parent = parent
                if self._agents:
                    self._agents.clear()
                self._parent_agent_calls.clear()
                self._pending_resolutions.clear()
                self._last_snapshot_key = None

            if parent is None or not parent.exists():
                if self._agents:
                    self._agents.clear()
                    self._emit([])
                return

            subagents_dir = parent.parent / parent.stem / "subagents"
            if not subagents_dir.is_dir():
                if self._agents:
                    self._agents.clear()
                    self._emit([])
                return

            seen_ids: set[str] = set()
            for jsonl in subagents_dir.glob("agent-*.jsonl"):
                agent_id = jsonl.stem[len("agent-") :]
                if not agent_id:
                    continue
                seen_ids.add(agent_id)
                entry = self._agents.get(agent_id)
                if entry is None:
                    entry = self._init_agent(agent_id, jsonl)
                    if entry is None:
                        continue
                    self._agents[agent_id] = entry
                self._tail_agent(entry, jsonl)

            # Agents whose files disappeared — rare.
            for gone in list(self._agents.keys() - seen_ids):
                del self._agents[gone]

            # Drain pending parent-resolved resolutions that arrived before the
            # matching subagent file was tracked. Typical when the tail thread
            # races ahead of the 2s scan cadence. Entries older than 60s with
            # no match are dropped — the matching subagent was either never
            # tracked (rejected before spawn) or finished via another path.
            now_wall = time.time()
            if self._pending_resolutions:
                unresolved: list[dict] = []
                for pr in self._pending_resolutions:
                    if self._apply_resolution_locked(pr):
                        continue
                    if now_wall - pr.get("queued_at", now_wall) > 60.0:
                        continue
                    unresolved.append(pr)
                self._pending_resolutions = unresolved

            # Fast-path idle fallback: a running agent whose final assistant
            # record ended with a text block and has been quiet for
            # IDLE_DONE_TIMEOUT_S seconds. Covers the null-stop_reason case
            # from L-2026-121.
            now = time.time()
            for entry in self._agents.values():
                state = entry["state"]
                if state.status != "running":
                    continue
                if not entry.get("ends_in_text"):
                    continue
                if now - state.last_activity_at > IDLE_DONE_TIMEOUT_S:
                    state.status = "done"
                    state.current_tool = ""

            # Safety-net idle fallback: any running agent quiet far longer
            # than STALE_DONE_TIMEOUT_S is assumed dead. Primary signal is the
            # parent tool_result (see note_parent_record); this catches the
            # corner cases where that signal never arrives.
            for entry in self._agents.values():
                state = entry["state"]
                if state.status != "running":
                    continue
                if now - state.last_activity_at > STALE_DONE_TIMEOUT_S:
                    state.status = "done"
                    state.current_tool = ""

            agents = [e["state"] for e in self._agents.values()]
            # Running (most recent activity first), then done (most recent first).
            agents.sort(key=lambda a: (a.status != "running", -a.last_activity_at))
            self._emit(agents)

    def snapshot(self) -> list[AgentState]:
        """Return the current agent list without going through dedup. Used
        by the app to re-push state after a frontend reload (Ctrl+R) so the
        chip strip doesn't go blank."""
        with self._lock:
            items = list(self._agents.values())
            agents = [e["state"] for e in items]
            agents.sort(key=lambda a: (a.status != "running", -a.last_activity_at))
            return agents

    # --- parent-JSONL signals ------------------------------------------------

    def note_parent_record(self, rec: dict) -> None:
        """Observe a record from the parent session JSONL.

        Called from the TranscriptTail thread for every parsed record (tail
        and replay). The two things we care about:

        1. An assistant `tool_use` block with `name="Agent"` — cache it by
           `tool_use_id`. This is what spawns a subagent.
        2. A user `tool_result` block whose `tool_use_id` matches a cached
           Agent spawn — the subagent has terminated (clean exit, rejection,
           or cancel). Mark the corresponding running subagent done.

        The subagent's own JSONL doesn't record the parent's tool_use_id, so
        we correlate by description + spawn-time proximity (oldest running
        agent with matching description wins).
        """
        if not isinstance(rec, dict):
            return
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return
        content = msg.get("content")
        if not isinstance(content, list):
            return

        rts = _parse_ts(rec.get("timestamp")) or time.time()
        role = msg.get("role")
        with self._lock:
            if role == "assistant":
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") != "tool_use":
                        continue
                    if b.get("name") != "Agent":
                        continue
                    tuid = b.get("id")
                    if not isinstance(tuid, str) or not tuid:
                        continue
                    if tuid in self._parent_agent_calls:
                        continue
                    inp = b.get("input") or {}
                    desc = inp.get("description") if isinstance(inp, dict) else ""
                    prompt = inp.get("prompt") if isinstance(inp, dict) else ""
                    self._parent_agent_calls[tuid] = {
                        "description": str(desc or ""),
                        "prompt": str(prompt or ""),
                        "spawn_ts": rts,
                        "resolved": False,
                    }
            elif role == "user":
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") != "tool_result":
                        continue
                    tuid = b.get("tool_use_id")
                    if not isinstance(tuid, str) or not tuid:
                        continue
                    call = self._parent_agent_calls.get(tuid)
                    if call is None or call.get("resolved"):
                        continue
                    call["resolved"] = True
                    resolution = {
                        "description": call["description"],
                        "parent_spawn_ts": call["spawn_ts"],
                        "seen_ts": rts,
                        "queued_at": time.time(),
                    }
                    if not self._apply_resolution_locked(resolution):
                        self._pending_resolutions.append(resolution)

    def _apply_resolution_locked(self, resolution: dict) -> bool:
        """Mark the best-matching running subagent done. Caller holds _lock.

        Returns True if a matching subagent was found and marked, False if
        no match exists yet (queue it in _pending_resolutions for next scan).
        """
        desc = resolution["description"]
        parent_ts = resolution["parent_spawn_ts"]
        best_id: Optional[str] = None
        best_key: Optional[tuple[float, float]] = None
        for agent_id, entry in self._agents.items():
            state = entry["state"]
            if state.status != "running":
                continue
            if entry.get("resolved_by_parent"):
                continue
            # Prefer exact description match; fall back to empty-description
            # agents (not yet init'd with meta) only if no desc match exists.
            if desc and state.description and state.description != desc:
                continue
            # Rank candidates: closest spawn_ts to parent_spawn_ts wins.
            key = (abs(state.started_at - parent_ts), state.started_at)
            if best_key is None or key < best_key:
                best_key = key
                best_id = agent_id
        if best_id is None:
            return False
        entry = self._agents[best_id]
        entry["resolved_by_parent"] = True
        state = entry["state"]
        state.status = "done"
        state.current_tool = ""
        return True

    def _emit(self, agents: list[AgentState]) -> None:
        # Deduplicate emits: only push if something actually changed.
        key = json.dumps([a.to_dict() for a in agents], sort_keys=True)
        if key == self._last_snapshot_key:
            return
        self._last_snapshot_key = key
        try:
            self._on_update(agents, self._session_id_fn())
        except Exception as e:  # noqa: BLE001
            print(f"[subagent-tracker] on_update failed: {e}", file=sys.stderr)

    # --- per-agent -----------------------------------------------------------

    def _init_agent(self, agent_id: str, jsonl: Path) -> Optional[dict]:
        meta_path = jsonl.with_suffix(".meta.json")
        subagent_type = ""
        description = ""
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(meta, dict):
                    subagent_type = str(meta.get("agentType") or "")
                    description = str(meta.get("description") or "")
            except (OSError, ValueError):
                pass
        try:
            started_at = jsonl.stat().st_ctime
        except OSError:
            return None
        state = AgentState(
            agent_id=agent_id,
            subagent_type=subagent_type,
            description=description,
            model="",
            prompt_preview="",
            started_at=started_at,
            last_activity_at=started_at,
            current_tool="",
            tokens=TokenTotals(),
        )
        return {
            "pos": 0,
            "buf": "",
            "state": state,
            "ends_in_text": False,
            "resolved_by_parent": False,
        }

    def _tail_agent(self, entry: dict, jsonl: Path) -> None:
        try:
            size = jsonl.stat().st_size
        except OSError:
            return
        if size < entry["pos"]:
            # File truncated / rewritten — reparse from the top.
            entry["pos"] = 0
            entry["buf"] = ""
        try:
            with jsonl.open("rb") as f:
                f.seek(entry["pos"])
                chunk = f.read()
                entry["pos"] = f.tell()
        except OSError:
            return
        if not chunk:
            return
        entry["buf"] += chunk.decode("utf-8", errors="replace")
        lines = entry["buf"].split("\n")
        entry["buf"] = lines[-1]
        for line in lines[:-1]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            self._apply_record(entry, rec)

    def _apply_record(self, entry: dict, rec: dict) -> None:
        state = entry["state"]
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None:
            state.last_activity_at = ts

        msg = rec.get("message")
        if not isinstance(msg, dict):
            return
        role = msg.get("role")
        content = msg.get("content")

        # First user record carries the spawn prompt as a plain string.
        if role == "user" and isinstance(content, str) and not state.prompt_preview:
            state.prompt_preview = content.strip()[:300]

        if role == "user":
            # A new user/tool_result turn means the model will respond again,
            # so any prior "ends in text" marker is no longer the last state.
            entry["ends_in_text"] = False

        if role == "assistant":
            model = msg.get("model")
            if isinstance(model, str) and not state.model:
                state.model = model

            usage = msg.get("usage")
            if isinstance(usage, dict):
                state.tokens.input += int(usage.get("input_tokens") or 0)
                state.tokens.output += int(usage.get("output_tokens") or 0)
                state.tokens.cache_read += int(usage.get("cache_read_input_tokens") or 0)
                state.tokens.cache_write += int(usage.get("cache_creation_input_tokens") or 0)

            saw_tool_use = False
            saw_text = False
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    btype = b.get("type")
                    if btype == "tool_use":
                        saw_tool_use = True
                        name = b.get("name")
                        if isinstance(name, str) and name:
                            state.current_tool = name
                            state.tool_histogram[name] = state.tool_histogram.get(name, 0) + 1
                    elif btype == "text":
                        txt = b.get("text")
                        if isinstance(txt, str) and txt.strip():
                            state.final_text = txt.strip()[:400]
                            saw_text = True

            entry["ends_in_text"] = saw_text and not saw_tool_use

            # Any terminal stop_reason flips to done. "tool_use" is the only
            # non-terminal value; null (missing) is handled by the idle-timeout
            # fallback in _scan_once.
            sr = msg.get("stop_reason")
            if isinstance(sr, str) and sr and sr != "tool_use":
                state.status = "done"
                state.current_tool = ""
