import json
from datetime import datetime, timezone
import os
import sys
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.session import SessionId
from core.utils.atomic import write_json_atomic

_globals_lock = threading.Lock()

BUDGETER_DIR = Path(__file__).parent.parent
LOG_PATH = BUDGETER_DIR / "data" / "usage_log.jsonl"
TMP_DIR = BUDGETER_DIR / "tmp"
CONFIG_PATH = BUDGETER_DIR / "config.json"

_DEFAULT_LOG_PATH = LOG_PATH
_DEFAULT_TMP_DIR = TMP_DIR


def _assert_isolated_in_test_mode(path, kind):
    """When APIARY_BUDGETER_TEST_ISOLATION=1, refuse to write to the default
    production paths. Tests must redirect either via configure_for_project(cwd)
    (subprocess hooks with .claude/budgeter.json) or by patching the module
    globals directly. Any write that slips through is a test-isolation bug.
    """
    if os.environ.get("APIARY_BUDGETER_TEST_ISOLATION") != "1":
        return
    defaults = {
        "log": _DEFAULT_LOG_PATH,
        "tmp": _DEFAULT_TMP_DIR,
    }
    default = defaults[kind]
    if Path(path).resolve() == Path(default).resolve():
        raise RuntimeError(
            f"budgeter test-isolation violation: write to default {kind} path {default} "
            f"while APIARY_BUDGETER_TEST_ISOLATION=1. "
            f"Redirect via configure_for_project(cwd) or patch the module global."
        )

# Project-level config filename placed inside a project's .claude/ directory
_PROJECT_CONFIG_FILENAME = "budgeter.json"


@contextmanager
def _file_lock(path):
    """
    Advisory cross-platform file lock around append operations.
    Uses a sibling .lock file.  Retries for up to 2 seconds before giving up
    (proceeding unlocked) so a crashed process never permanently blocks writes.
    """
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 2.0
    acquired = False
    fd = None
    try:
        while time.monotonic() < deadline:
            try:
                fd = open(lock_path, "x", encoding="utf-8")  # exclusive create — atomic on most FS
                acquired = True
                break
            except FileExistsError:
                time.sleep(0.05)
        yield
    finally:
        if acquired and fd is not None:
            try:
                fd.close()
            except OSError:
                pass
            try:
                lock_path.unlink()
            except OSError:
                pass


def _resolve_safe_project_root(cwd):
    """
    Resolve cwd to a real, absolute path and return it.
    Raises ValueError if the path is empty, non-absolute after resolution,
    or contains suspicious traversal components that survive normalization.
    """
    if not cwd or not str(cwd).strip():
        raise ValueError("cwd is empty")
    p = Path(cwd).resolve()
    # Must be absolute after resolution
    if not p.is_absolute():
        raise ValueError(f"cwd resolved to a non-absolute path: {p}")
    return p


def configure_for_project(cwd):
    """
    If a .claude/budgeter.json exists in cwd, update the module-level path
    globals atomically (under a lock) and return True.  Falls back to
    module-level defaults and returns False when no project config is found.

    The globals are updated as a single assignment under _globals_lock so
    concurrent calls for different projects cannot interleave partial state.

    NOTE: Readers of LOG_PATH, TMP_DIR, and CONFIG_PATH do not hold
    _globals_lock. This is an intentional known gap — the globals are Path
    objects (reference assignment is atomic in CPython) and the practical
    concurrent-write window is extremely narrow (hook startup). A full reader
    lock would require threading.local() copies and is deferred.
    """
    global LOG_PATH, TMP_DIR, CONFIG_PATH
    if not cwd:
        return False
    try:
        root = _resolve_safe_project_root(cwd)
    except ValueError:
        return False
    project_config = root / ".claude" / _PROJECT_CONFIG_FILENAME
    if not project_config.exists():
        return False
    new_config = project_config
    new_log = root / ".claude" / "budgeter-log.jsonl"
    new_tmp = root / ".claude" / "budgeter-tmp"
    with _globals_lock:
        CONFIG_PATH = new_config
        LOG_PATH = new_log
        TMP_DIR = new_tmp
    return True


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def read_log():
    if not LOG_PATH.exists():
        return []
    entries = []
    with _file_lock(LOG_PATH):
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return entries


def count_tasks():
    """Count unique (session_id, task_turn) pairs in the log."""
    entries = read_log()
    tasks = set()
    for e in entries:
        key = (e.get("session_id", ""), e.get("task_turn", e.get("turn_number", 0)))
        tasks.add(key)
    return len(tasks)


def append_entry(entry):
    # Force-write if the entry is explicitly marked (e.g. compaction markers).
    # Otherwise drop entries where both deltas are zero — there is nothing to record.
    if not entry.get("_marker"):
        if entry.get("tokens_delta", 0) == 0 and entry.get("net_tokens_delta", 0) == 0:
            return
    _assert_isolated_in_test_mode(LOG_PATH, "log")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Strip internal-only fields before persisting
    persisted = {k: v for k, v in entry.items() if k != "_marker"}
    with _file_lock(LOG_PATH):
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(persisted) + "\n")


_MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024  # 50 MB — transcripts beyond this are unusually large


def read_session_jsonl(transcript_path):
    if not transcript_path:
        return []
    path = Path(transcript_path)
    if not path.exists():
        return []
    try:
        file_size = path.stat().st_size
    except OSError:
        return []
    if file_size > _MAX_TRANSCRIPT_BYTES:
        # Return an empty list rather than exhausting memory on a huge file.
        # Callers treat an empty result as "no history" which is safe.
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def get_user_turn_number(session_entries):
    """Count user messages with actual text content (not tool results).

    Tool result blocks have type 'tool_result', not 'text', but they can contain
    nested content lists that include text blocks.  We only count top-level 'text'
    blocks that are not inside a tool_result block.
    """
    count = 0
    for entry in session_entries:
        msg = entry.get("message", {})
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if isinstance(content, str) and content.strip():
                count += 1
            elif isinstance(content, list):
                # Only count if there is at least one top-level text block
                # (tool_result blocks have type "tool_result", not "text")
                if any(
                    isinstance(b, dict)
                    and b.get("type") == "text"
                    for b in content
                ):
                    count += 1
    return count


def get_user_message_at_turn(session_entries, turn_number):
    """Return the text of the user message at the given turn number (1-indexed)."""
    count = 0
    for entry in session_entries:
        msg = entry.get("message", {})
        if msg.get("role") == "user":
            content = msg.get("content", [])
            text = ""
            if isinstance(content, str) and content.strip():
                text = content.strip()
                count += 1
            elif isinstance(content, list):
                text_blocks = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if text_blocks:
                    text = " ".join(text_blocks).strip()
                    count += 1
            if count == turn_number:
                return text
    return ""


def get_last_assistant_message(session_entries):
    """Return the text content of the last assistant message in the session."""
    last_text = ""
    for entry in session_entries:
        msg = entry.get("message", {})
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        last_text = block.get("text", "")
            elif isinstance(content, str):
                last_text = content
    return last_text


def iter_api_calls(session_entries):
    """Yield one ``usage`` dict per API call, in transcript order.

    Claude Code writes one JSONL line per content block (thinking, text,
    tool_use ...) and every line of the same API turn repeats the same
    ``message.id`` and the same ``usage``. Summing per line therefore counts
    a multi-block turn 2-3x (review B2). Records are deduped on
    ``message.id``; a record without an id is counted on its own.
    """
    seen = set()
    for entry in session_entries:
        msg = entry.get("message", {})
        if msg.get("role") != "assistant":
            continue
        msg_id = msg.get("id")
        if msg_id:
            if msg_id in seen:
                continue
            seen.add(msg_id)
        usage = msg.get("usage") or {}
        if isinstance(usage, dict):
            yield usage


def _usage_total(usage):
    """Everything the API billed for one call: uncached input, cache reads,
    cache writes (``cache_creation_input_tokens`` — never counted before
    review B5), and output."""
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("output_tokens", 0)
    )


def get_cumulative_tokens(session_entries):
    """Sum token usage across all API calls in the session (deduped per call)."""
    return sum(_usage_total(u) for u in iter_api_calls(session_entries))


def get_last_call_tokens(session_entries):
    """Return ``(input, cache_read, cache_creation, output)`` of the most
    recent API call, or all zeros when the transcript has none."""
    last = None
    for usage in iter_api_calls(session_entries):
        last = usage
    if last is None:
        return 0, 0, 0, 0
    return (
        last.get("input_tokens", 0),
        last.get("cache_read_input_tokens", 0),
        last.get("cache_creation_input_tokens", 0),
        last.get("output_tokens", 0),
    )


def build_cost_entry(baseline, session_id, transcript_path, tokens_now,
                     last_input, last_cache, last_create, last_output):
    """Build the log entry attributing the API call(s) since *baseline* to
    the tool recorded in it. Shared by the PRE and Stop hooks.

    ``net_tokens_delta`` is the marginal cost: growth of the prompt (uncached
    input + cache reads + cache writes, summed *before* clamping so tokens
    that merely moved from "written" to "read" between calls net to zero)
    plus the new output. The split fields are informational.
    """
    tokens_delta = max(0, tokens_now - baseline["tokens"])
    prev_input = baseline.get("baseline_input", 0)
    prev_cache = baseline.get("baseline_cache", 0)
    prev_create = baseline.get("baseline_cache_creation", 0)
    if prev_input > 0 or prev_cache > 0 or prev_create > 0:
        input_growth = max(0, last_input - prev_input)
        cache_growth = max(0, last_cache - prev_cache)
        create_growth = max(0, last_create - prev_create)
        prompt_growth = max(
            0, (last_input + last_cache + last_create) - (prev_input + prev_cache + prev_create)
        )
        net_tokens_delta = prompt_growth + last_output
    else:
        input_growth = cache_growth = create_growth = 0
        context_tokens = baseline.get("context_tokens", 0)
        net_tokens_delta = max(0, tokens_delta - context_tokens)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "tool_name": baseline.get("prev_tool_name", ""),
        "assistant_message": baseline.get("prev_assistant_message", ""),
        "user_message": baseline.get("user_message", ""),
        "tokens_delta": tokens_delta,
        "context_tokens": prev_input + prev_cache + prev_create + baseline.get("baseline_output", 0),
        "net_tokens_delta": net_tokens_delta,
        "input_tokens_delta": input_growth,
        "cache_tokens_delta": cache_growth,
        "cache_creation_tokens_delta": create_growth,
        "output_tokens_delta": last_output,
        "turn_number": baseline.get("turn_number", 0),
        "task_turn": baseline.get("task_turn", baseline.get("turn_number", 0)),
        "project": str(Path(transcript_path).parent) if transcript_path else "",
    }


def _sid(session_id):
    """Coerce raw string to SessionId if needed."""
    return session_id if isinstance(session_id, SessionId) else SessionId(session_id)


# Bump when the meaning of a baseline's numbers changes. Schema 2 (2026-08):
# token totals are deduped per API call and include cache creation, so a
# schema-1 baseline is 1.7-2.6x too large to compare against — comparing
# produced a spurious "[compaction]" marker on the first PRE after upgrade.
BASELINE_SCHEMA = 2


def baseline_comparable(baseline) -> bool:
    """True when *baseline* was written by code that counts the same way we
    do now, so a delta against it means something."""
    return isinstance(baseline, dict) and baseline.get("schema") == BASELINE_SCHEMA


def load_baseline(session_id):
    """Return the session's baseline dict, or None when there is none.

    A baseline that cannot be parsed (a hook killed mid-write, a disk
    hiccup) is treated as absent rather than raised: the next PRE rewrites
    it and the session carries on with one gap in the log. Before review
    B1 the JSONDecodeError escaped every hook, and since nothing rewrote
    the file the session's hooks stayed broken for good.
    """
    path = _sid(session_id).tmp_path("baseline.json", TMP_DIR)
    if not path.exists():
        return None
    with _file_lock(path):
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"[budgeter] unreadable baseline {path.name}: {exc}; starting over",
                  file=sys.stderr)
            return None
    return data if isinstance(data, dict) and "tokens" in data else None


def save_baseline(session_id, tokens, context_tokens=0, prev_tool_name="", prev_assistant_message="", turn_number=0, task_turn=None, user_message="", baseline_input=0, baseline_cache=0, baseline_output=0, agent_description="", baseline_cache_creation=0):
    _assert_isolated_in_test_mode(TMP_DIR, "tmp")
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    path = _sid(session_id).tmp_path("baseline.json", TMP_DIR)
    with _file_lock(path):
        # Write to a sibling temp file and os.replace it in, so a hook killed
        # mid-write can never leave a truncated baseline behind (review B1).
        write_json_atomic(path, {
                "schema": BASELINE_SCHEMA,
                "tokens": tokens,
                "context_tokens": context_tokens,
                "baseline_input": baseline_input,
                "baseline_cache": baseline_cache,
                "baseline_cache_creation": baseline_cache_creation,
                "baseline_output": baseline_output,
                "prev_tool_name": prev_tool_name,
                "prev_assistant_message": prev_assistant_message,
                "turn_number": turn_number,
                "task_turn": task_turn if task_turn is not None else turn_number,
                "user_message": user_message,
                "agent_description": agent_description,
        })


def cleanup_session(session_id):
    sid = _sid(session_id)
    # "pending.json" is a snapshot file the deleted warning feature wrote;
    # keep sweeping it so an upgrade does not strand one per old session.
    for suffix in ["pending.json", "baseline.json"]:
        p = sid.tmp_path(suffix, TMP_DIR)
        if p.exists():
            try:
                p.unlink()
            except PermissionError:
                pass  # file locked by another process (common on Windows); leave it
        # Orphaned atomic-write temp files (a hook killed between mkstemp and
        # os.replace, or a Windows replace refused while the file was open).
        for orphan in p.parent.glob(p.name + ".*.tmp") if p.parent.exists() else ():
            try:
                orphan.unlink()
            except OSError:
                pass
