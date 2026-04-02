import json
from pathlib import Path

BUDGETER_DIR = Path(__file__).parent.parent
LOG_PATH = BUDGETER_DIR / "data" / "usage_log.jsonl"
FEEDBACK_PATH = BUDGETER_DIR / "data" / "feedback.jsonl"
TMP_DIR = BUDGETER_DIR / "tmp"
CONFIG_PATH = BUDGETER_DIR / "config.json"

# Project-level config filename placed inside a project's .claude/ directory
_PROJECT_CONFIG_FILENAME = "budgeter.json"


def configure_for_project(cwd):
    """
    If a .claude/budgeter.json exists in cwd, switch all paths to that project's
    directory. Returns True if project-level config was found, False otherwise.
    """
    global LOG_PATH, FEEDBACK_PATH, TMP_DIR, CONFIG_PATH
    if not cwd:
        return False
    project_config = Path(cwd) / ".claude" / _PROJECT_CONFIG_FILENAME
    if not project_config.exists():
        return False
    CONFIG_PATH = project_config
    LOG_PATH = Path(cwd) / ".claude" / "budgeter-log.jsonl"
    FEEDBACK_PATH = Path(cwd) / ".claude" / "budgeter-feedback.jsonl"
    TMP_DIR = Path(cwd) / ".claude" / "budgeter-tmp"
    return True


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def read_log():
    if not LOG_PATH.exists():
        return []
    entries = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def count_entries():
    return len(read_log())


def count_tasks():
    """Count unique (session_id, task_turn) pairs in the log."""
    entries = read_log()
    tasks = set()
    for e in entries:
        key = (e.get("session_id", ""), e.get("task_turn", e.get("turn_number", 0)))
        tasks.add(key)
    return len(tasks)


def append_entry(entry):
    if entry.get("tokens_delta", 0) == 0:
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def append_feedback(entry):
    FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_feedback():
    if not FEEDBACK_PATH.exists():
        return []
    entries = []
    with open(FEEDBACK_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def read_session_jsonl(transcript_path):
    if not transcript_path:
        return []
    path = Path(transcript_path)
    if not path.exists():
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
    """Count user messages with actual text content (not tool results)."""
    count = 0
    for entry in session_entries:
        msg = entry.get("message", {})
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if isinstance(content, str) and content.strip():
                count += 1
            elif isinstance(content, list):
                if any(isinstance(b, dict) and b.get("type") == "text" for b in content):
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


def get_cumulative_tokens(session_entries):
    """Sum all token usage across all assistant messages in the session."""
    total = 0
    for entry in session_entries:
        msg = entry.get("message", {})
        if msg.get("role") == "assistant":
            usage = msg.get("usage", {})
            total += usage.get("input_tokens", 0)
            total += usage.get("output_tokens", 0)
            total += usage.get("cache_read_input_tokens", 0)
    return total


def get_last_call_tokens(session_entries):
    """Return (input_tokens, cache_read_tokens, output_tokens) of the most recent assistant message."""
    last_input = 0
    last_cache = 0
    last_output = 0
    for entry in session_entries:
        msg = entry.get("message", {})
        if msg.get("role") == "assistant":
            usage = msg.get("usage", {})
            last_input = usage.get("input_tokens", 0)
            last_cache = usage.get("cache_read_input_tokens", 0)
            last_output = usage.get("output_tokens", 0)
    return last_input, last_cache, last_output


def save_snapshot(session_id, snapshot):
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    path = TMP_DIR / f"{session_id}_pending.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f)


def load_snapshot(session_id):
    path = TMP_DIR / f"{session_id}_pending.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def delete_snapshot(session_id):
    path = TMP_DIR / f"{session_id}_pending.json"
    if path.exists():
        path.unlink()


def load_baseline(session_id):
    path = TMP_DIR / f"{session_id}_baseline.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_baseline(session_id, tokens, context_tokens=0, prev_tool_name="", prev_assistant_message="", turn_number=0, task_turn=None, user_message="", scope_flags=None, predicted_cost=0, warning_fired=False, baseline_input=0, baseline_cache=0, baseline_output=0):
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    path = TMP_DIR / f"{session_id}_baseline.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "tokens": tokens,
            "context_tokens": context_tokens,
            "baseline_input": baseline_input,
            "baseline_cache": baseline_cache,
            "baseline_output": baseline_output,
            "prev_tool_name": prev_tool_name,
            "prev_assistant_message": prev_assistant_message,
            "turn_number": turn_number,
            "task_turn": task_turn if task_turn is not None else turn_number,
            "user_message": user_message,
            "scope_flags": scope_flags if scope_flags is not None else [],
            "predicted_cost": predicted_cost,
            "warning_fired": warning_fired,
        }, f)


def cleanup_session(session_id):
    for name in [f"{session_id}_pending.json", f"{session_id}_baseline.json"]:
        p = TMP_DIR / name
        if p.exists():
            p.unlink()
