import re
from collections import defaultdict

_DEFAULT_SCOPE_KEYWORDS = [
    "refactor", "rewrite", "migrate", "overhaul", "restructure", "redesign",
]
_DEFAULT_BREADTH_KEYWORDS = [
    "entire", "throughout", "across", "codebase", "everywhere",
]
_DEFAULT_INVESTIGATIVE_KEYWORDS = [
    "why", "explain", "understand", "investigate", "debug", "diagnose", "analyze",
]
_APPROVAL_PATTERNS = re.compile(
    r'^\s*('
    r'y(es|ep|eah|a)?'
    r'|ok(ay)?'
    r'|sure'
    r'|proceed'
    r'|go\s*(ahead|for\s*it)?'
    r'|do\s*it'
    r'|sounds?\s*good'
    r'|that\s*works?'
    r'|approved?'
    r'|lgtm'
    r'|confirm(ed)?'
    r'|please'
    r'|lets?\s*(do|go)'
    r')\s*[.!,]?\s*$',
    re.IGNORECASE
)


def is_approval_message(text):
    """Return True if the user message is a short approval/confirmation."""
    if not text or not text.strip():
        return False
    # Must be short — long messages with "yes" embedded aren't approvals
    if len(text.split()) > 8:
        return False
    return bool(_APPROVAL_PATTERNS.match(text.strip()))
_FILE_EXTENSIONS = (
    "py|js|ts|json|md|txt|yaml|yml|css|html|jsx|tsx|go|rs|java|rb|sh|vue|php|c|cpp|h"
)
# Match filenames/relative paths but NOT URLs (no scheme://) and not bare
# path components inside a URL.  We disallow forward-slash in the stem so
# that "https://example.com/foo.js" does not match as a file reference — only
# the final path component "foo.js" would qualify if it appears as a word boundary.
_FILE_PATTERN = re.compile(
    r'(?<![:/])(?<!\w/)\b[\w\-\.]*[\w\-]\.(?:' + _FILE_EXTENSIONS + r')\b'
)
_STEP_PATTERNS = [
    r'\bthen\b', r'\bnext\b', r'\balso\b', r'\bfirst\b', r'\bfinally\b',
    # Match numbered list items only: digit(s) followed by a period and whitespace
    # (or end of string), not bare decimals like "3.14" or version strings.
    r'(?m)^\s*\d+\.\s',
]


def score_flags(flags, config):
    """
    Compute the weighted score for a list of triggered rule names.
    Unrecognized rules default to weight 1.0.
    Falls back to len(flags) if no rule_weights config is present.
    """
    weights = config.get("rule_weights", {})
    if not weights:
        return float(len(flags))
    return sum(weights.get(f, 1.0) for f in flags)


def _median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def detect_scope_flags(text, config, user_text=None):
    """
    Run rule-based scope detection on the assistant planning message (text) and
    optionally the user message (user_text).

    - scope_keywords, breadth_keywords, file_count, step_count fire on `text`
    - investigative_keywords fires on `user_text` (skipped if user_text is None)

    Returns a list of triggered rule names. Each rule hit = 1 point toward
    the warn_score_threshold.
    """
    flags = []

    if text and text.strip():
        lower = text.lower()
        words = set(re.findall(r'\b\w+\b', lower))

        scope_kw = set(config.get("scope_keywords", _DEFAULT_SCOPE_KEYWORDS))
        if words & scope_kw:
            flags.append("scope_keywords")

        breadth_kw = set(config.get("breadth_keywords", _DEFAULT_BREADTH_KEYWORDS))
        if words & breadth_kw:
            flags.append("breadth_keywords")

        file_threshold = config.get("file_count_threshold", 3)
        file_mentions = set(_FILE_PATTERN.findall(lower))
        if len(file_mentions) >= file_threshold:
            flags.append("file_count")

        step_threshold = config.get("step_count_threshold", 4)
        step_count = sum(len(re.findall(p, lower)) for p in _STEP_PATTERNS)
        if step_count >= step_threshold:
            flags.append("step_count")

    if user_text is not None and user_text.strip():
        user_lower = user_text.lower()
        user_words = set(re.findall(r'\b\w+\b', user_lower))
        investigative_kw = set(config.get("investigative_keywords", _DEFAULT_INVESTIGATIVE_KEYWORDS))
        if user_words & investigative_kw:
            flags.append("investigative_keywords")

    return flags


def _group_tasks(log_entries, config):
    """
    Group log entries by (session_id, task_turn). For each group, take the first
    entry's assistant_message as the representative planning message, backfill
    scope_flags by running rules against it, and sum net_tokens_delta.

    Returns a list of (flags, total_cost) tuples.
    """
    groups = {}
    for e in log_entries:
        if "turn_number" not in e:
            continue
        # Skip Agent-sourced entries logged with task_turn=0 — these are
        # background agent costs not attributed to any user task and would
        # artificially inflate the cost of task 0, skewing predictions.
        if e.get("tool_name") == "Agent" and e.get("task_turn", 0) == 0:
            continue
        task_turn = e.get("task_turn", e["turn_number"])
        key = (e.get("session_id", ""), task_turn)
        if key not in groups:
            # Store the representative message AND any scope_flags from the first entry
            groups[key] = {
                "message": e.get("assistant_message", ""),
                "total": 0,
                "scope_flags": e.get("scope_flags"),  # may be None or []
            }
        else:
            # Prefer the first non-empty scope_flags we encounter for this group
            if not groups[key]["scope_flags"] and e.get("scope_flags"):
                groups[key]["scope_flags"] = e["scope_flags"]
        groups[key]["total"] += e.get("net_tokens_delta", 0)

    tasks = []
    for g in groups.values():
        # Prefer stored scope_flags if present and non-empty; otherwise backfill from message.
        stored_flags = g.get("scope_flags")
        task_flags = stored_flags if stored_flags else detect_scope_flags(g["message"], config)
        tasks.append((task_flags, g["total"]))
    return tasks


def estimate_magnitude(current_flags, log_entries, config):
    """
    Given the current task's scope flags, find historical flagged tasks that share
    at least one rule and return their median cost.

    Falls back to all historically flagged tasks if the overlapping subset is too small.

    Returns:
        (median_cost, sample_size, fallback_used)
        median_cost is 0 if there is not enough history.
    """
    if not current_flags or not log_entries:
        return 0, 0, False

    min_flagged = config.get("min_flagged_tasks", 10)
    current_set = set(current_flags)

    historical_tasks = _group_tasks(log_entries, config)

    # Primary: tasks that share at least one rule with the current task
    matching_costs = [
        cost for flags, cost in historical_tasks
        if set(flags) & current_set
    ]

    if len(matching_costs) >= min_flagged:
        return _median(matching_costs), len(matching_costs), False

    # Fallback: all historically flagged tasks
    all_flagged_costs = [cost for flags, cost in historical_tasks if flags]
    if len(all_flagged_costs) >= min_flagged:
        return _median(all_flagged_costs), len(all_flagged_costs), True

    return 0, 0, False
