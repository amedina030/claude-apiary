"""Machine-wide Claude Code transcript reader for usage attribution.

Every Claude Code invocation on this machine — interactive sessions,
subagents, and the ``claude -p`` calls the runner and the scheduled pipelines
make — writes a JSONL transcript under ``~/.claude/projects/<key>/``. Each
assistant record carries the model and the exact ``usage`` the API billed for
that call. That makes the transcripts the one complete source for "where did
the usage go", independent of which repos have apiary hooks installed.

This is a different quantity from what the budgeter hooks log:
``net_tokens_delta`` is the marginal growth of the prompt between two tool
calls (the session-length nudge's input), while the limits are drawn down by
the full billed input of every call, cache reads included. The 2026-09-05
survey (T-2026-315 lineage) found the hooks had logged 2.7M tokens for a week
of interactive apiary work that billed 89.7M.

Layout facts this module relies on (L-2026-118):

* ``<key>/<session>.jsonl`` is the main transcript; ``<key>/<session>/
  subagents/agent-*.jsonl`` are that session's subagent sidechains.
* Every line of one API turn repeats the same ``message.id`` and ``usage``
  (one line per content block), so calls are deduplicated on the id.
* User records carry ``entrypoint`` — ``"sdk-cli"`` for headless ``claude -p``
  runs, ``"cli"`` for interactive sessions — and ``cwd``.

Weights: ``load`` is tokens weighted per model by the ``model_weights`` table
in ``budgeter/config.json``. The shipped weights are Anthropic's API list-price
ratios, used purely as relative weights so a Sonnet call and a Fable call can
be compared; nothing here is a bill and the subscription's limit formula is
not public. ``budgeter/usage_calibrate.py`` replaces the weights with measured
percent-of-limit figures once usage samples exist.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

PROJECTS_DIR = Path.home() / ".claude" / "projects"

HEADLESS_ENTRYPOINT = "sdk-cli"
SYNTHETIC_MODEL = "<synthetic>"

# Records whose text is harness plumbing rather than something a person typed.
_PLUMBING_PREFIXES = ("<local-command", "<command-", "<system-reminder", "<task-notification")

DEFAULT_WEIGHTS = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}
DEFAULT_CACHE_READ_FACTOR = 0.1
DEFAULT_CACHE_WRITE_FACTOR = 1.25

TOKEN_KEYS = ("input", "cache_write", "cache_read", "output")


@dataclass
class Weights:
    models: dict
    cache_read_factor: float = DEFAULT_CACHE_READ_FACTOR
    cache_write_factor: float = DEFAULT_CACHE_WRITE_FACTOR

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "Weights":
        config = config or {}
        models = config.get("model_weights")
        if not isinstance(models, dict) or not models:
            models = DEFAULT_WEIGHTS
        return cls(
            models={k: v for k, v in models.items() if isinstance(v, dict)},
            cache_read_factor=float(config.get("cache_read_factor", DEFAULT_CACHE_READ_FACTOR)),
            cache_write_factor=float(config.get("cache_write_factor", DEFAULT_CACHE_WRITE_FACTOR)),
        )

    def entry_for(self, model: str) -> Optional[dict]:
        """Exact match first, then the longest key the model id starts with
        (``claude-haiku-4-5-20251001`` -> ``claude-haiku-4-5``)."""
        if model in self.models:
            return self.models[model]
        best = None
        for key, entry in self.models.items():
            if model.startswith(key) and (best is None or len(key) > len(best[0])):
                best = (key, entry)
        return best[1] if best else None

    def load(self, model: str, tokens: dict) -> float:
        """Weighted load of one call's token counts; 0 for an unweighted model."""
        entry = self.entry_for(model)
        if entry is None:
            return 0.0
        pin = float(entry.get("input", 0.0))
        pout = float(entry.get("output", 0.0))
        cache_read = entry.get("cache_read")
        read_rate = float(cache_read) if cache_read is not None else pin * self.cache_read_factor
        return (
            tokens["input"] * pin
            + tokens["cache_write"] * pin * self.cache_write_factor
            + tokens["cache_read"] * read_rate
            + tokens["output"] * pout
        ) / 1e6


def zero_tokens() -> dict:
    return {k: 0 for k in TOKEN_KEYS}


def tokens_from_usage(usage: dict) -> dict:
    return {
        "input": int(usage.get("input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
    }


def add_tokens(into: dict, tokens: dict) -> None:
    for k in TOKEN_KEYS:
        into[k] += tokens[k]


def total_tokens(tokens: dict) -> int:
    return sum(tokens[k] for k in TOKEN_KEYS)


@dataclass
class Call:
    ts: datetime
    model: str
    tokens: dict
    sidechain: bool = False


@dataclass
class Session:
    key: str  # "<project key>/<session id>"
    session_id: str
    project_dir: str
    cwd: str = ""
    headless: bool = False
    first_prompt: str = ""
    calls: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return project_label(self.cwd, self.project_dir)

    @property
    def kind(self) -> str:
        return "headless" if self.headless else "interactive"


def parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def parse_since(text: str, now: Optional[datetime] = None) -> datetime:
    """``7d`` / ``36h`` / ``90m`` relative to now, or an ISO date/datetime."""
    now = now or datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([dhm])", text.strip().lower())
    if m:
        n = int(m.group(1))
        unit = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}
        return now - unit[m.group(2)]
    ts = parse_ts(text.strip())
    if ts is None:
        raise ValueError(f"not a duration (7d, 36h, 90m) or ISO date: {text!r}")
    return ts


_WORKTREE_MARKERS = (
    (".runner-worktrees", "runner worktrees"),
    (".claude/worktrees", "agent worktrees"),
)


def project_label(cwd: str, project_dir: str) -> str:
    """Human label for a session's project.

    Sessions in a repo's runner worktrees, agent worktrees or Claude Code
    scratchpad collapse onto the repo with a suffix, so a week of nightly
    runner work reads as one row instead of one row per worktree.
    """
    path = (cwd or "").replace("\\", "/")
    if path:
        for marker, suffix in _WORKTREE_MARKERS:
            idx = path.find("/" + marker + "/")
            if idx == -1 and path.endswith("/" + marker):
                idx = len(path) - len(marker) - 1
            if idx != -1:
                repo = path[:idx].rstrip("/").rsplit("/", 1)[-1]
                return f"{repo} ({suffix})"
        # Claude Code scratchpads live at <Temp>/claude/<project key>/<session>/scratchpad.
        m = re.search(r"/Temp/claude/([^/]+)/[0-9a-f-]{36}/scratchpad(?:/|$)", path)
        if m:
            return f"{_unmangle(m.group(1))} (scratchpad)"
        return _repo_name(path)
    return _unmangle(project_dir)


def _repo_name(path: str) -> str:
    """Name of the repo containing *path*: the nearest ancestor with a
    ``.git`` entry, else the last path segment. A session started in a
    subdirectory (``finances/relay/data/staging``) is still the repo's."""
    try:
        p = Path(path)
        if p.exists():
            for candidate in (p, *p.parents):
                if (candidate / ".git").exists():
                    return candidate.name or path
    except OSError:
        pass
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _unmangle(key: str) -> str:
    """Best-effort readable name for a Claude Code project key when no cwd
    was recorded: drop the drive/prefix segments and keep the last one."""
    parts = [p for p in key.split("-") if p]
    return parts[-1] if parts else key


def _first_prompt_text(rec: dict) -> Optional[str]:
    msg = rec.get("message") or {}
    if msg.get("role") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    content = msg.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return None
    text = text.strip()
    if not text or text.startswith(_PLUMBING_PREFIXES):
        return None
    return " ".join(text.split())[:160]


def _read_records(path: Path) -> Iterator[dict]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def _ingest(session: Session, path: Path, since: datetime, until: Optional[datetime], seen: set):
    sidechain = "subagents" in path.parts
    for rec in _read_records(path):
        if not session.cwd and isinstance(rec.get("cwd"), str):
            session.cwd = rec["cwd"]
        if rec.get("entrypoint") == HEADLESS_ENTRYPOINT:
            session.headless = True
        if not session.first_prompt and not sidechain and rec.get("type") == "user":
            text = _first_prompt_text(rec)
            if text:
                session.first_prompt = text
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not usage:
            continue
        model = str(msg.get("model") or "?")
        if model == SYNTHETIC_MODEL:
            # Claude Code's own placeholder turns (aborts, local commands):
            # zero usage, no model, nothing to attribute.
            continue
        msg_id = msg.get("id") or rec.get("uuid")
        if msg_id in seen:
            continue
        seen.add(msg_id)
        ts = parse_ts(rec.get("timestamp"))
        if ts is None or ts < since or (until is not None and ts > until):
            continue
        session.calls.append(
            Call(ts=ts, model=model, tokens=tokens_from_usage(usage), sidechain=sidechain)
        )


def _touched_since(path: Path, since: datetime) -> bool:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= since
    except OSError:
        return False


def iter_sessions(
    since: datetime,
    until: Optional[datetime] = None,
    projects_dir: Optional[Path] = None,
) -> Iterator[Session]:
    """Yield every session with at least one API call inside the window.

    A session is its main transcript plus its ``subagents/`` sidechains;
    subagent calls count toward the parent session. Files not modified since
    *since* are skipped without being opened.
    """
    root = Path(projects_dir or PROJECTS_DIR)
    if not root.is_dir():
        return
    for project in sorted(p for p in root.iterdir() if p.is_dir()):
        main_files = {p.stem: p for p in project.glob("*.jsonl")}
        subagent_dirs = {p.name: p for p in project.iterdir() if (p / "subagents").is_dir()}
        for session_id in sorted(set(main_files) | set(subagent_dirs)):
            files = []
            main = main_files.get(session_id)
            if main is not None and _touched_since(main, since):
                files.append(main)
            sub_dir = subagent_dirs.get(session_id)
            if sub_dir is not None:
                files.extend(
                    p
                    for p in sorted((sub_dir / "subagents").glob("*.jsonl"))
                    if _touched_since(p, since)
                )
            if not files:
                continue
            session = Session(
                key=f"{project.name}/{session_id}",
                session_id=session_id,
                project_dir=project.name,
            )
            seen: set = set()
            # Main file first so cwd / entrypoint / first prompt come from it.
            if main is not None and main in files:
                _ingest(session, main, since, until, seen)
            for path in files:
                if path != main:
                    _ingest(session, path, since, until, seen)
            if session.calls:
                session.calls.sort(key=lambda c: c.ts)
                yield session


@dataclass
class Bucket:
    name: str
    tokens: dict = field(default_factory=zero_tokens)
    load: float = 0.0
    calls: int = 0
    sessions: set = field(default_factory=set)
    models: set = field(default_factory=set)
    kinds: dict = field(default_factory=lambda: {"interactive": 0.0, "headless": 0.0})
    first_prompt: str = ""
    label: str = ""  # project label, filled for session buckets

    def add(self, session: Session, call: Call, load: float) -> None:
        add_tokens(self.tokens, call.tokens)
        self.load += load
        self.calls += 1
        self.sessions.add(session.key)
        self.models.add(call.model)
        self.kinds[session.kind] += load

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "load": round(self.load, 4),
            "tokens": dict(self.tokens),
            "total_tokens": total_tokens(self.tokens),
            "calls": self.calls,
            "sessions": len(self.sessions),
            "models": sorted(self.models),
            "interactive_load": round(self.kinds["interactive"], 4),
            "headless_load": round(self.kinds["headless"], 4),
            "first_prompt": self.first_prompt,
            "label": self.label,
        }


def _bucket_key(by: str, session: Session, call: Call) -> str:
    if by == "project":
        return session.label
    if by == "session":
        return session.key
    if by == "model":
        return call.model
    if by == "day":
        return call.ts.astimezone().strftime("%Y-%m-%d")
    if by == "kind":
        return session.kind
    raise ValueError(f"unknown grouping: {by}")


def aggregate(sessions, weights: Weights, by: str = "project") -> dict:
    """Group calls into Buckets keyed by *by*; returns ``{key: Bucket}``.

    Also records which models had no weight so the report can say so
    instead of silently under-counting them.
    """
    buckets: dict = {}
    unweighted: set = set()
    for session in sessions:
        for call in session.calls:
            load = weights.load(call.model, call.tokens)
            if weights.entry_for(call.model) is None:
                unweighted.add(call.model)
            key = _bucket_key(by, session, call)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = buckets[key] = Bucket(name=key)
                if by == "session":
                    bucket.first_prompt = session.first_prompt
                    bucket.label = session.label
            bucket.add(session, call, load)
    buckets["__unweighted__"] = sorted(unweighted)  # type: ignore[assignment]
    return buckets
