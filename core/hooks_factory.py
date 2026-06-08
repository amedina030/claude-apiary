"""Hook entry builders for apiary's per-repo install.

Constructs the per-tool ``hooks`` dicts that ``apiary install`` writes
into each bootstrapped repo's ``.claude/settings.json``. Pulled out of
``setup.py`` so the install path doesn't have to import the now-stub
setup module. See ``MIGRATION-PLAN.md`` §10 phase 5 for context.

Hook commands always go through the per-repo launcher
(``$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py``) post-migration.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.hooks_lib import hook_cmd

PYTHON = Path(sys.executable)

BUDGETER_DIR = REPO_ROOT / "budgeter"
SCRIBE_DIR = REPO_ROOT / "scribe"
DOCS_DIR = REPO_ROOT / "docs"
CORE_DIR = REPO_ROOT / "core"


def _per_repo_kwargs() -> dict:
    """Common keyword args for ``hook_cmd`` in per-repo install mode."""
    return {"repo_root": REPO_ROOT, "per_repo_launcher": True}


def build_budgeter_hooks() -> dict:
    """Return the budgeter PreToolUse / PostToolUse / Stop hook entries.

    Reads the monitored-tools list from ``budgeter/config.json`` (same
    set the legacy global install used). Falls back to ``("Agent", "Bash")``
    if the file is missing or malformed.
    """
    try:
        with open(BUDGETER_DIR / "config.json", encoding="utf-8") as f:
            tools = json.load(f).get("monitored_tools", ["Agent", "Bash"])
    except (FileNotFoundError, json.JSONDecodeError):
        tools = ["Agent", "Bash"]
    kw = _per_repo_kwargs()
    pre_cmd = hook_cmd(BUDGETER_DIR / "hooks" / "pre_tool_use.py", PYTHON, **kw)
    post_cmd = hook_cmd(BUDGETER_DIR / "hooks" / "post_tool_use.py", PYTHON, **kw)
    stop_cmd = hook_cmd(BUDGETER_DIR / "hooks" / "stop_session.py", PYTHON, **kw)
    return {
        "PreToolUse": [
            {"matcher": tool, "hooks": [{"type": "command", "command": pre_cmd}]}
            for tool in tools
        ],
        "PostToolUse": [
            {"matcher": tool, "hooks": [{"type": "command", "command": post_cmd}]}
            for tool in tools
        ],
        "Stop": [
            {"hooks": [{"type": "command", "command": stop_cmd}]}
        ],
    }


def build_core_hooks() -> dict:
    """Core-tool hooks: session-id injection, install check, startup
    rules drift, learnings injector, error reminder."""
    kw = _per_repo_kwargs()
    prompt_startup_cmd = hook_cmd(CORE_DIR / "hooks" / "startup_prompt_hook.py", PYTHON, **kw)
    install_cmd = hook_cmd(CORE_DIR / "hooks" / "check_install.py", PYTHON, **kw)
    session_cmd = hook_cmd(CORE_DIR / "hooks" / "inject_session.py", PYTHON, **kw)
    startup_cmd = hook_cmd(CORE_DIR / "hooks" / "startup_hook.py", PYTHON, **kw)
    stop_cmd = hook_cmd(CORE_DIR / "hooks" / "check_install_stop.py", PYTHON, **kw)
    error_reminder_cmd = hook_cmd(CORE_DIR / "hooks" / "context_rule_error_reminder.py", PYTHON, **kw)
    learnings_inject_cmd = hook_cmd(CORE_DIR / "hooks" / "learnings_inject_hook.py", PYTHON, **kw)
    research_reminder_cmd = hook_cmd(CORE_DIR / "hooks" / "research_capture_reminder.py", PYTHON, **kw)
    return {
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": prompt_startup_cmd}]},
        ],
        "PreToolUse": [
            {"matcher": "", "hooks": [{"type": "command", "command": install_cmd}]},
            {"matcher": "", "hooks": [{"type": "command", "command": session_cmd}]},
            {"matcher": "", "hooks": [{"type": "command", "command": startup_cmd}]},
            {"matcher": "Edit", "hooks": [{"type": "command", "command": learnings_inject_cmd}]},
            {"matcher": "Write", "hooks": [{"type": "command", "command": learnings_inject_cmd}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": learnings_inject_cmd}]},
            # Research-capture reminder: nudge to persist durable findings via
            # the researcher. Matches web-research tools plus the subagent tool
            # (Agent here, Task in stock Claude Code) so research run inside a
            # subagent is still caught — the hook fires in the parent at spawn.
            {"matcher": "WebSearch|WebFetch|Agent|Task",
             "hooks": [{"type": "command", "command": research_reminder_cmd}]},
        ],
        "PostToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": error_reminder_cmd}]},
        ],
        "Stop": [
            {"hooks": [{"type": "command", "command": stop_cmd}]}
        ],
    }


def build_scribe_hooks() -> dict:
    """Scribe transcript-saver Stop hook."""
    save_cmd = hook_cmd(
        CORE_DIR / "hooks" / "save_transcript.py", PYTHON, **_per_repo_kwargs(),
    )
    return {
        "Stop": [
            {"hooks": [{"type": "command", "command": save_cmd}]},
        ],
    }


def build_docs_hooks() -> dict:
    """Docs standards-reminder PreToolUse hook."""
    remind_cmd = hook_cmd(
        DOCS_DIR / "hooks" / "remind_standards.py", PYTHON, **_per_repo_kwargs(),
    )
    return {
        "PreToolUse": [
            {"matcher": "", "hooks": [{"type": "command", "command": remind_cmd}]},
        ],
    }
