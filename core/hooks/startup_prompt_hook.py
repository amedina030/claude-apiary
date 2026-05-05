#!/usr/bin/env python3
"""
UserPromptSubmit hook — injects startup context on the first user message.

Always injects: identity, notes summary, learnings, CLI reference.

Runner subprocesses (auto_refine, auto_plan, auto_harden, executor,
approval) set ``APIARY_RUNNER_SUBPROCESS=1`` to skip injection — they
are one-shot workers that don't use any of this context, and the
injection is tens of KB of input tokens per spawn.
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.session import SessionId
from core.hook_context import context_block, hook_allow, read_payload
from core.sanitizer import sanitize_and_report
from core.startup import run_init, run_summary
from scribe.notes import _git_repo_root, _use_repo_layout, STATE_LAYOUT_ENV

# Sanitizer hit log lives in the umbrella state directory. With the
# centralized .repos/ layout, this resolves to
# <apiary>/.repos/<name>-<id>/hooks/ — per-target observability. Falls
# back to <apiary-repo>/.apiary/hooks/ when the env var is not set so
# the legacy in-repo layout still works during the migration window.
def _hook_state_dir() -> Path:
    env_dir = os.environ.get("APIARY_TARGET_STATE_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return PROJECT_ROOT / ".apiary"


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


def main():
    try:
        _run()
    except Exception:
        # Hooks must not crash — degrade to no context
        hook_allow(event="UserPromptSubmit")


def _run():
    # Runner subprocesses (auto_refine, auto_plan, etc.) set this env var
    # to skip the entire startup injection. Saves tens of KB of input tokens
    # per spawn — none of identity/notes/learnings/CLI-index is useful to a
    # one-shot runner worker.
    if os.environ.get("APIARY_RUNNER_SUBPROCESS") == "1":
        hook_allow(event="UserPromptSubmit")
        return

    payload = read_payload()

    raw_id = payload.get("session_id", "")
    if not raw_id:
        hook_allow(event="UserPromptSubmit")
        return

    try:
        sid = SessionId(raw_id)
    except ValueError:
        hook_allow(event="UserPromptSubmit")
        return

    # Run-once guard
    flag_file = sid.flag_path("startup_prompt_done")
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    if flag_file.exists():
        hook_allow(event="UserPromptSubmit")
        return
    flag_file.write_text("1", encoding="utf-8")

    parts = []
    first_message = payload.get("message", "")
    cwd = payload.get("cwd", str(PROJECT_ROOT))

    # Repo-layout gate. The default centralized layout resolves scribe state
    # via git rev-parse on the session's cwd → registry → <state-dir>/scribe/.
    # Sessions started outside a git repo skip notes/summary/learnings
    # injection rather than falsely loading apiary's own state. The
    # APIARY_STATE_LAYOUT=legacy escape hatch reads from
    # ~/.claude/projects/<project_key>/.
    repo_layout = _use_repo_layout()
    session_repo_root = _git_repo_root(Path(cwd)) if repo_layout else None
    skip_notes_injection = repo_layout and session_repo_root is None

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

    # --- 2. Summary: active notes, latest handoff ---
    role = identity.get("role", "user")
    mission = identity.get("mission", "general")
    if skip_notes_injection:
        parts.append("")
        parts.append(
            "summary: skipped (APIARY_STATE_LAYOUT=repo and session cwd "
            "is not inside a git repo)"
        )
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
            if repo_layout:
                learnings_env[STATE_LAYOUT_ENV] = "repo"
                learnings_cwd = str(session_repo_root)
            else:
                learnings_cwd = str(PROJECT_ROOT)
            result = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scribe" / "notes.py"),
                 "learnings", "--index"],
                capture_output=True, text=True, timeout=5,
                cwd=learnings_cwd,
                env=learnings_env,
            )
            if result.returncode == 0 and result.stdout.strip():
                scrubbed, hits = sanitize_and_report(result.stdout.strip())
                _log_sanitizer_hits("learnings", hits, sid.full)
                parts.append("")
                parts.append("--- learnings index (run `python scribe/notes.py get L-X` for full body) ---")
                parts.append(scrubbed)
        except Exception:
            pass

    # --- 4. CLI index (compact; use cli_lookup.py for full details) ---
    try:
        cli_index = (PROJECT_ROOT / "docs" / "reference" / "cli-index.md").read_text(encoding="utf-8")
        scrubbed, hits = sanitize_and_report(cli_index.strip())
        _log_sanitizer_hits("cli_index", hits, sid.full)
        parts.append("")
        parts.append("--- cli-tools index (run `python docs/reference/cli_lookup.py <tool>` for full flags) ---")
        parts.append(scrubbed)
    except Exception:
        pass

    hook_allow(context_block("startup", *parts), event="UserPromptSubmit")


if __name__ == "__main__":
    main()
