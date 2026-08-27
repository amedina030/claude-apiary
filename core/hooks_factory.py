"""Hook entry builders for apiary's per-repo install.

Constructs the ``hooks`` dict that ``apiary install`` writes into each
bootstrapped repo's ``.claude/settings.json``. See
``docs/architecture/per-repo-install.md`` for context.

**One entry per event.** Until 2026-08 this module emitted one settings.json
entry per hook per matcher — 11 PreToolUse + 5 PostToolUse + 2 Stop — and each
of those spawned two interpreters through the launcher (~18 per Bash tool
call, ≈1.7 s; review X-1). Now every event points at a single dispatcher,
``core/hooks/dispatch.py <verb>``, which reads the payload once and runs the
whole chain in-process. The per-hook matchers moved into the dispatcher's
registry (``core.hooks.dispatch._registry``) — that is where a new hook is
registered now, not here.

Hook commands always go through the per-repo launcher
(``$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py``) post-migration.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.hooks_lib import hook_cmd, resolve_python

# Honors the APIARY_PYTHON override; falls back to the running interpreter.
PYTHON = resolve_python()

CORE_DIR = REPO_ROOT / "core"
DISPATCHER = CORE_DIR / "hooks" / "dispatch.py"

# settings.json event name -> the dispatcher verb that handles it. Mirrors
# ``core.hooks.dispatch.EVENTS``; the parity test in
# ``core/hooks/test_dispatch.py`` keeps the two from drifting apart.
EVENT_VERBS: dict[str, str] = {
    "PreToolUse": "pre",
    "PostToolUse": "post",
    "Stop": "stop",
    "UserPromptSubmit": "prompt",
}


def dispatch_cmd(verb: str) -> str:
    """The launcher command that runs the dispatcher for one event verb."""
    return hook_cmd(
        DISPATCHER,
        PYTHON,
        repo_root=REPO_ROOT,
        per_repo_launcher=True,
        args=(verb,),
    )


def build_dispatch_hooks() -> dict:
    """Return the whole settings.json ``hooks`` dict: one entry per event.

    The matcher is empty (every tool) because the dispatcher re-applies each
    hook's matcher in-process against ``tool_name`` — a hook whose matcher does
    not match is never even imported.
    """
    hooks: dict[str, list] = {}
    for event, verb in EVENT_VERBS.items():
        entry: dict = {"hooks": [{"type": "command", "command": dispatch_cmd(verb)}]}
        if event in ("PreToolUse", "PostToolUse"):
            entry = {"matcher": "", **entry}
        hooks[event] = [entry]
    return hooks
