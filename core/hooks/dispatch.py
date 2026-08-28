#!/usr/bin/env python3
"""One hook process per Claude Code event (review X-1 / §5 Phase 3.1).

Before this, every hook was its own ``settings.json`` entry and every entry
went through the per-repo launcher, which spawned a *second* interpreter for
the script: a `Bash` tool call fired 7 PreToolUse + 2 PostToolUse hooks =
**~18 interpreter starts ≈ 1.7 s**, roughly half of them no-ops that read the
payload, saw a tool name they don't care about, and printed ``{}``.

The dispatcher is registered once per event with an empty matcher. It reads
the payload from stdin **once** and runs every relevant hook module
in-process, in the documented order, then merges what they said into a single
JSON response. With the launcher's ``runpy`` change (``core/launcher_template``)
that is **one** interpreter start per event — two per tool call, counting
PostToolUse.

Contract. Every hook module exposes ``run(payload) -> HookResult | None``
(:class:`core.hook_context.HookResult`). ``None`` means "no opinion" — the
common case. ``HookResult.context`` is merged into the one
``additionalContext`` block; ``HookResult.block_reason`` is a gate's decision
to stop the call, which the dispatcher turns into the ``deny`` JSON + exit 2
that ``hook_context.hook_block`` emits, skipping every hook after it. Hooks
never vote ``allow`` (review C-1) — the dispatcher has no way to express one.

Isolation. Each hook runs inside its own ``try/except``: an exception is
appended to ``<repo>/.claude/apiary/hooks.log`` (rotated at 1 MiB) and the
chain continues. One broken hook can no longer wedge a session, and — unlike
the old fan-out — the failure is no longer invisible.

Matchers. Claude Code's per-entry ``matcher`` regex is gone from
settings.json, so the dispatcher re-applies it in-process against
``tool_name``: a hook whose matcher does not match is never imported, which is
what keeps the merged process cheap on a `Read` call.

Usage (via the per-repo launcher)::

    python .claude/apiary/launch.py core/hooks/dispatch.py pre
    python .claude/apiary/launch.py core/hooks/dispatch.py post|stop|prompt|session-start
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.hook_context import HookResult, hook_allow, hook_block, join_contexts  # noqa: E402

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

# CLI verb -> Claude Code hookEventName. The verb is what settings.json passes.
EVENTS: dict[str, str] = {
    "pre": "PreToolUse",
    "post": "PostToolUse",
    "stop": "Stop",
    "prompt": "UserPromptSubmit",
    "session-start": "SessionStart",
}

# Where per-hook failures go. Rotated (single generation) at this size.
LOG_NAME = "hooks.log"
LOG_MAX_BYTES = 1024 * 1024  # 1 MiB


class Hook(NamedTuple):
    """One registry row.

    - *name*: what shows up in ``hooks.log``; also the handle tests patch by.
    - *module*: dotted path, or a repo-relative ``.py`` path for the one hook
      that lives outside a package (``docs/hooks/`` has no ``__init__.py``).
    - *matcher*: regex matched (fullmatch) against ``tool_name``. ``""``/None
      means "every tool", exactly as in settings.json. Ignored for events
      that carry no tool name (Stop, UserPromptSubmit, SessionStart).
    """

    name: str
    module: str
    matcher: str | None = None


# Tools the budgeter logs, mirrored from budgeter/config.json at dispatch time
# so the in-process matcher stays identical to the one the old per-tool
# settings.json entries used.
_BUDGETER_DEFAULT_TOOLS = ("Agent", "Bash")


def budgeter_matcher() -> str:
    """``Agent|Bash|Read|Write`` — the budgeter's monitored-tools alternation."""
    try:
        with open(REPO_ROOT / "budgeter" / "config.json", encoding="utf-8") as f:
            tools = json.load(f).get("monitored_tools") or _BUDGETER_DEFAULT_TOOLS
    except (OSError, json.JSONDecodeError):
        tools = _BUDGETER_DEFAULT_TOOLS
    return "|".join(re.escape(str(t)) for t in tools)


def _registry() -> dict[str, tuple[Hook, ...]]:
    """The hook chain per event, in execution order.

    Order matters and is documented in ``docs/reference/hooks.md``: the drift
    check runs first so everything after it sees an up-to-date self-pointer and
    registry entry, then core, budgeter, scribe, docs — the order
    ``core/install.py`` used to write into settings.json.
    """
    budgeter = budgeter_matcher()
    return {
        "PreToolUse": (
            Hook("drift_check", "core.hooks.per_repo_drift_check"),
            Hook("inject_session", "core.hooks.inject_session"),
            Hook("learnings_inject", "core.hooks.learnings_inject_hook", "Edit|Write|Bash"),
            Hook(
                "research_reminder",
                "core.hooks.research_capture_reminder",
                "WebSearch|WebFetch|Agent|Task",
            ),
            Hook("pre_push_doc_conformer", "core.hooks.pre_push_doc_conformer", "Bash"),
            Hook("pre_push_secret_scan", "core.hooks.pre_push_secret_scan", "Bash"),
            Hook("budgeter_pre", "budgeter.hooks.pre_tool_use", budgeter),
            Hook("remind_standards", "docs/hooks/remind_standards.py", "Write|Edit"),
            # duplicate-helper hook goes here — §5a-C(2). Before Write/Edit,
            # if the content defines a function whose name already exists
            # elsewhere in the repo, inject "X already exists at path:line —
            # reuse or say why not." Non-blocking; indexes definitions once
            # per session. Add it as Hook("duplicate_helper", ..., "Write|Edit")
            # — nothing else in this file needs to change.
        ),
        "PostToolUse": (
            Hook("context_rule_error_reminder", "core.hooks.context_rule_error_reminder", "Bash"),
            Hook("budgeter_post", "budgeter.hooks.post_tool_use", budgeter),
        ),
        "Stop": (
            Hook("budgeter_stop", "budgeter.hooks.stop_session"),
            Hook("save_transcript", "core.hooks.save_transcript"),
        ),
        "UserPromptSubmit": (Hook("startup_prompt", "core.hooks.startup_prompt_hook"),),
        # No SessionStart hooks yet; the verb exists so one can be registered
        # without another settings.json migration.
        "SessionStart": (),
    }


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log_path() -> Path:
    """``<repo>/.claude/apiary/hooks.log`` for the repo the hook fired in.

    Resolution order matches ``core.flags._per_repo_root``: ``CLAUDE_PROJECT_DIR``
    (set by Claude Code), then ``APIARY_TARGET_REPO`` (set by the launcher),
    then cwd — never the home directory (review S1).
    """
    for env in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
        val = os.environ.get(env, "").strip()
        if val and Path(val).is_dir():
            return Path(val) / ".claude" / "apiary" / LOG_NAME
    return Path.cwd() / ".claude" / "apiary" / LOG_NAME


def _rotate(path: Path) -> None:
    """Keep one previous generation once the log passes ``LOG_MAX_BYTES``."""
    try:
        if path.stat().st_size < LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        try:  # Windows: the .1 may be held open elsewhere
            path.unlink()
        except OSError:
            pass


def log_failure(event: str, hook_name: str, exc: BaseException) -> None:
    """Append one hook failure to ``hooks.log``. Never raises.

    The old fan-out swallowed these (``except Exception: pass``) — a hook could
    be dead for months and nothing said so. The dispatcher still fails open,
    but no longer silently.
    """
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {event} {hook_name}: {exc!r}\n{detail}\n")
    except Exception:  # noqa: BLE001 — observability must never break a session
        pass


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def matches(matcher: str | None, tool_name: str) -> bool:
    """Claude Code's matcher semantics, re-applied in-process.

    Empty / missing / ``*`` matches every tool; anything else is a regex
    fullmatched against the tool name. A malformed regex falls back to an
    equality test rather than skipping the hook — better to run a hook than
    to lose it to a typo.
    """
    if not matcher or matcher == "*":
        return True
    try:
        return re.fullmatch(matcher, tool_name or "") is not None
    except re.error:
        return matcher == tool_name


def load_run(module: str) -> Callable[[dict], HookResult | None]:
    """Import a hook module and return its ``run``.

    Accepts a dotted path, or a repo-relative ``.py`` path for hooks outside a
    package (``docs/hooks/`` ships no ``__init__.py``, and adding one would
    make ``docs`` an importable package it has no business being).
    """
    if module.endswith(".py"):
        path = REPO_ROOT / module
        name = "apiary_hook_" + module.replace("/", "_").replace("\\", "_")[:-3]
        existing = sys.modules.get(name)
        if existing is not None:
            return existing.run
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load hook module from {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(name, None)  # never cache a half-executed module
            raise
        return mod.run
    return importlib.import_module(module).run


def dispatch(event: str, payload: dict, hooks: tuple[Hook, ...] | None = None) -> HookResult:
    """Run every hook registered for *event* and merge what they said.

    Returns the merged :class:`HookResult`. A gate's ``block_reason`` short-
    circuits the chain — the hooks after it do not run, exactly as exit 2 used
    to stop Claude Code from consulting them.
    """
    if hooks is None:
        hooks = _registry().get(event, ())
    tool_name = payload.get("tool_name") or ""
    contexts: list[str] = []

    for hook in hooks:
        if not matches(hook.matcher, tool_name):
            continue
        try:
            result = load_run(hook.module)(payload)
            if result is None:
                continue
            if not isinstance(result, HookResult):
                # A hook that returns something else is a bug in that hook, not
                # a reason to take the chain down: log it and move on.
                raise TypeError(
                    f"{hook.name}.run returned {type(result).__name__}, expected HookResult | None"
                )
            block_reason, context = result.block_reason, result.context
        except SystemExit as exc:
            # A hook that still calls sys.exit() (a leftover standalone
            # habit) would otherwise end the process here with no JSON and
            # silently skip every later hook. Exit 2 keeps its meaning (a
            # block); anything else is a failure of that hook alone.
            code = exc.code if isinstance(exc.code, int) else 1
            if code == 2:
                return HookResult(block_reason=f"{hook.name} exited 2 without a reason")
            log_failure(event, hook.name, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — fail open, per hook
            log_failure(event, hook.name, exc)
            continue
        if block_reason:
            return HookResult(block_reason=block_reason)
        if context:
            contexts.append(context)

    return HookResult(context=join_contexts(*contexts) or None)


def main(argv: list[str] | None = None) -> int:
    """Read one payload from stdin, run the event's chain, print one response."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in EVENTS:
        print(f"usage: dispatch.py {{{'|'.join(EVENTS)}}}", file=sys.stderr)
        return 2
    event = EVENTS[argv[0]]

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        # Unreadable payload: no opinion, exit 0. Nothing downstream can be
        # decided from a payload we could not parse.
        log_failure(event, "<payload>", exc)
        hook_allow(event=event)
        return 0
    if not isinstance(payload, dict):
        hook_allow(event=event)
        return 0

    result = dispatch(event, payload)
    if result.block_reason:
        hook_block(result.block_reason, event=event)  # prints, exits 2
    hook_allow(result.context or None, event=event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
