#!/usr/bin/env python3
"""
UserPromptSubmit hook — injects startup context on the first user message.

Always injects: identity, notes summary, learnings, CLI reference, the
apiary toolkit rules (the same content the /apiary-context skill serves,
read from core/commands/apiary-context.md), and — when the session cwd is a
registered git repo — the compass rule table (``<state-dir>/compass/rules.md``,
D-2026-62). The latter two used to depend on the model invoking
/apiary-context; injecting them here makes them deterministic. The skill
remains for on-demand reload (e.g. /clear). ``core/hooks/compass_rules.py``
pins the table's principle rows to every tenth user message.

Runner subprocesses (auto_refine, auto_plan, auto_harden, executor,
approval) set ``APIARY_RUNNER_SUBPROCESS=1`` to skip injection — they
are one-shot workers that don't use any of this context, and the
injection is tens of KB of input tokens per spawn. They receive the rule
table as a prompt preamble from ``runner/claude_subprocess.py`` instead.
"""

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import frontmatter
from core.hook_context import HookResult, context_block, run_standalone
from core.sanitizer import sanitize_and_report
from core.session import SessionId
from core.startup import run_init, run_summary
from core.utils.gitutil import git_root
from core.utils.state import find_state_dir, state_dir_from_env


def _context_body(md: str) -> str:
    """Return the markdown body with the leading dialect header removed."""
    parts = frontmatter.split(md)
    if parts is None:
        return md
    return parts[1].lstrip("\n")


# Sanitizer hit log lives in the umbrella state directory. With the
# centralized .repos/ layout, this resolves to
# <apiary>/.repos/<name>-<id>/hooks/ — per-target observability. Falls
# back to <apiary-repo>/.apiary/hooks/ when the env var is not set so
# the legacy in-repo layout still works during the migration window.
def _hook_state_dir() -> Path:
    return state_dir_from_env() or (PROJECT_ROOT / ".apiary")


def _log_sanitizer_hits(site: str, hits: dict[str, int], session_id: str) -> None:
    """Append one JSONL line when the sanitizer scrubbed at least one pattern.

    Silent no-op when hits is empty. Silent no-op on any write failure —
    hooks must not crash, and missing observability is better than a broken
    first-turn context.
    """
    if not hits:
        return
    try:
        state_dir = _hook_state_dir()
        log_path = state_dir / "hooks" / "sanitizer_debug.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        gitignore = state_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "session_id": session_id,
            "site": site,
            "hits": hits,
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def run(payload: dict):
    """Build the first-message startup context block for this session."""
    # Runner subprocesses (auto_refine, auto_plan, etc.) set this env var
    # to skip the entire startup injection. Saves tens of KB of input tokens
    # per spawn — none of identity/notes/learnings/CLI-index is useful to a
    # one-shot runner worker.
    if os.environ.get("APIARY_RUNNER_SUBPROCESS") == "1":
        return None

    raw_id = payload.get("session_id", "")
    if not raw_id:
        return None

    try:
        sid = SessionId(raw_id)
    except ValueError:
        return None

    # Run-once guard
    flag_file = sid.flag_path("startup_prompt_done")
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    if flag_file.exists():
        return None
    flag_file.write_text("1", encoding="utf-8")

    parts = []
    first_message = payload.get("message", "")
    cwd = payload.get("cwd", str(PROJECT_ROOT))

    # The centralized state layout resolves scribe state via git rev-parse
    # on the session's cwd → registry → <state-dir>/scribe/. Sessions started
    # outside a git repo skip notes/summary/learnings injection rather than
    # falsely loading apiary's own state.
    session_repo_root = git_root(Path(cwd))
    skip_notes_injection = session_repo_root is None

    # --- 1. Init: identity ---
    identity = {}
    try:
        init_result = run_init(sid.full, first_message, cwd)
        identity = init_result.get("identity", {})

        parts.append(
            f"identity: role={identity.get('role', 'user')}, "
            f"mission={identity.get('mission', 'general')}, "
            f"registered={identity.get('registered', True)}, "
            f"wants={identity.get('wants_role', 'user')}/{identity.get('wants_mission', 'general')}"
        )
    except Exception:
        parts.append("init: failed (using defaults)")

    # --- 1b. Surface: flag GUI-hosted sessions ---
    # gui/session.py sets APIARY_GUI_SESSION=1 in the spawned claude's env
    # (inherited by this hook subprocess) when the session runs inside the GUI.
    if os.environ.get("APIARY_GUI_SESSION") == "1":
        parts.append("")
        parts.append(
            "surface: this session is running inside the apiary GUI (a pywebview "
            "desktop app), not a raw terminal. The user reads your output in the "
            "GUI chat pane and types into the GUI composer. Edits to gui/web/* "
            "require a full GUI restart (not a reload) to take effect. Do NOT use "
            "the AskUserQuestion multiple-choice tool in this surface — its "
            "option-picker does not render reliably in the GUI; ask any questions "
            "as plain text in your reply instead."
        )

    # --- 2. Summary: active notes, latest handoff ---
    role = identity.get("role", "user")
    mission = identity.get("mission", "general")
    if skip_notes_injection:
        parts.append("")
        parts.append("summary: skipped (session cwd is not inside a git repo)")
    else:
        try:
            summary_text = run_summary(cwd, role, mission)
            if summary_text:
                scrubbed, hits = sanitize_and_report(summary_text)
                _log_sanitizer_hits("summary", hits, sid.full)
                parts.append("")
                parts.append(scrubbed)
        except Exception:
            parts.append("summary: failed (non-critical)")

    # --- 3. Learnings (compact tag-grouped index; full bodies load on-demand) ---
    # Was `learnings --full` (~46KB of prose); now `--index` (~10KB tag-grouped).
    # Claude fetches individual bodies via `notes.py get L-X` or via the
    # PreToolUse learnings_inject_hook when editing a file in a tagged area.
    if not skip_notes_injection:
        try:
            learnings_env = os.environ.copy()
            learnings_cwd = str(session_repo_root)
            result = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scribe" / "notes.py"), "learnings", "--index"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=learnings_cwd,
                env=learnings_env,
            )
            if result.returncode == 0 and result.stdout.strip():
                scrubbed, hits = sanitize_and_report(result.stdout.strip())
                _log_sanitizer_hits("learnings", hits, sid.full)
                parts.append("")
                parts.append(
                    "--- learnings index (run `python scribe/notes.py get L-X` for full body) ---"
                )
                parts.append(
                    "The tag groups below are this repo's known traps. Before "
                    "changing files or running commands in an area a tag "
                    "covers, `get` the matching bodies first — the titles "
                    "here are truncated; do not act on them alone."
                )
                parts.append(scrubbed)
        except Exception:
            pass

    # --- 4. CLI index (compact; use cli_lookup.py for full details) ---
    try:
        cli_index = (PROJECT_ROOT / "docs" / "reference" / "cli-index.md").read_text(
            encoding="utf-8"
        )
        scrubbed, hits = sanitize_and_report(cli_index.strip())
        _log_sanitizer_hits("cli_index", hits, sid.full)
        parts.append("")
        # The launcher idiom, not a bare relative path: the session's cwd is
        # the target repo, not main-apiary, so `python docs/...` only resolves
        # in this one checkout (review §5 Phase 5).
        parts.append(
            '--- cli-tools index (run `python "$(git rev-parse --show-toplevel)'
            '/.claude/apiary/launch.py" docs/reference/cli_lookup.py <tool>` '
            "for full flags) ---"
        )
        parts.append(scrubbed)
    except Exception:
        pass

    # --- 5. Apiary toolkit rules (static guardrails) ---
    # Injected from the same markdown the /apiary-context skill serves, so the
    # launcher convention, portability, scribe decision tree, and reference
    # subsystems are guaranteed present before the first tool call — instead of
    # depending on the model remembering to invoke the skill. The skill remains
    # for on-demand reload (e.g. after /clear).
    try:
        ctx_md = (PROJECT_ROOT / "core" / "commands" / "apiary-context.md").read_text(
            encoding="utf-8"
        )
        ctx_body = _context_body(ctx_md.strip())
        if ctx_body:
            scrubbed, hits = sanitize_and_report(ctx_body)
            _log_sanitizer_hits("apiary_context", hits, sid.full)
            parts.append("")
            parts.append(
                "--- apiary toolkit rules (also available on demand via /apiary-context) ---"
            )
            parts.append(scrubbed)
    except Exception:
        pass

    # --- 6. Compass rule table (dynamic, per-target) ---
    # The only runtime-resolved piece of apiary-context: the second-person
    # rule table `compass/rules.py build` writes (D-2026-62). Read directly
    # here so it is guaranteed loaded rather than relying on the skill's
    # cat-if-exists snippet. The launcher's pre-resolved state dir comes
    # first; find_state_dir is read-only (no auto-registration), so it is
    # safe to call from a hook. compass_rules.py re-pins the principle rows
    # every tenth message.
    if not skip_notes_injection:
        try:
            state_dir = state_dir_from_env() or find_state_dir(session_repo_root)
            if state_dir is not None:
                rules_path = state_dir / "compass" / "rules.md"
                if rules_path.is_file():
                    scrubbed, hits = sanitize_and_report(
                        rules_path.read_text(encoding="utf-8").strip()
                    )
                    _log_sanitizer_hits("compass", hits, sid.full)
                    parts.append("")
                    parts.append(
                        "--- compass rules for Claude (mined from this user's corrections "
                        "and acceptances; explicit user statements and feedback memory "
                        "override; the principle rows are re-pinned every ten messages) ---"
                    )
                    parts.append(scrubbed)
        except Exception:
            pass

    return HookResult(context=context_block("startup", *parts))


if __name__ == "__main__":
    run_standalone(run, event="UserPromptSubmit")
