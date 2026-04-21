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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


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
    started_at: float        # epoch seconds
    last_activity_at: float  # epoch seconds
    current_tool: str
    tokens: TokenTotals
    tool_histogram: dict[str, int] = field(default_factory=dict)
    final_text: str = ""
    status: str = "running"  # "running" | "done"

    def to_dict(self) -> dict:
        d = asdict(self)
        # tokens is already a dict via asdict — rename keys the frontend uses.
        return d


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
        # {agent_id: {"pos": int, "buf": str, "state": AgentState}}
        self._agents: dict[str, dict] = {}
        self._last_snapshot_key: Optional[str] = None

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
        # Parent switched (e.g. /clear) → drop prior state but keep scanning.
        if parent != self._last_parent:
            self._last_parent = parent
            if self._agents:
                self._agents.clear()
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
            agent_id = jsonl.stem[len("agent-"):]
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

        agents = [e["state"] for e in self._agents.values()]
        # Running (most recent activity first), then done (most recent first).
        agents.sort(key=lambda a: (a.status != "running", -a.last_activity_at))
        self._emit(agents)

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
        return {"pos": 0, "buf": "", "state": state}

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
            self._apply_record(entry["state"], rec)

    def _apply_record(self, state: AgentState, rec: dict) -> None:
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

            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    btype = b.get("type")
                    if btype == "tool_use":
                        name = b.get("name")
                        if isinstance(name, str) and name:
                            state.current_tool = name
                            state.tool_histogram[name] = state.tool_histogram.get(name, 0) + 1
                    elif btype == "text":
                        txt = b.get("text")
                        if isinstance(txt, str) and txt.strip():
                            state.final_text = txt.strip()[:400]

            if msg.get("stop_reason") == "end_turn":
                state.status = "done"
                state.current_tool = ""
