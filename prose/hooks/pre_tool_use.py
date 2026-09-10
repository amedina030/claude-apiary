#!/usr/bin/env python3
"""PreToolUse hook: the prose rules before the first note or document write, findings on every one.

Two jobs, both aimed at the moment the text is being produced rather than at
session start, where a rule read once is thousands of tokens behind by the
time it matters:

1. **Instructions before.** On the session's first ``Write``/``Edit`` of a
   Markdown file, or its first ``scribe/notes.py add``/``learn`` command,
   inject the compact rule list (one line per warning-or-above rule, rendered
   from the rule files by ``prose.engine.summary_lines`` so it cannot drift)
   and the check command. Once per session, flag file under ``session-tmp/``.

2. **Findings on the way in.** On every ``Write`` (``content``) or ``Edit``
   (``new_string``) of a Markdown path the config includes, run the checker
   and inject the warning-or-above findings as context, capped so a long
   document does not flood the turn. When the per-repo ``prose-gate`` flag
   is on and an error-level finding exists, **block** the write with the
   findings as the reason. Scribe notes get the same treatment from
   ``scribe/notes.py`` itself, since the note body arrives as a CLI argument
   the hook cannot see cleanly.

Never raises: the dispatcher logs an exception and the write proceeds. Skipped
for runner subprocesses (one-shot workers, no reminder audience).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.hook_context import HookResult, context_block, run_standalone
from core.session import SessionId
from core.utils.gitutil import git_root
from prose import engine, gate

REMINDER_FLAG = "prose_reminded"
#: Findings injected per write before the rest are summarised as a count.
MAX_FINDINGS_IN_CONTEXT = 12
MARKDOWN_SUFFIXES = (".md", ".markdown")
CHECK_COMMAND = (
    'python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" prose/cli.py check <file>'
)

_NOTES_SCRIPT_RE = re.compile(r"(?:^|[\\/\s\"'])notes\.py$")


def _session(payload: dict) -> "SessionId | None":
    raw = str(payload.get("session_id") or "").strip()
    if not raw:
        return None
    try:
        return SessionId(raw)
    except ValueError:
        return None


def is_note_write(command: str) -> bool:
    """True when a Bash command runs ``scribe/notes.py add`` or ``learn``.

    Token-based, like the learnings injector: the launcher idiom puts
    ``notes.py`` a few tokens in, and the verb is the next positional token
    after it. A command that merely mentions the words does not count.
    """
    if not command or "notes.py" not in command:
        return False
    tokens = command.replace("\n", " ").split()
    for i, tok in enumerate(tokens):
        if _NOTES_SCRIPT_RE.search(tok.strip("\"'")):
            for nxt in tokens[i + 1 :]:
                if nxt.startswith("-"):
                    continue
                return nxt in ("add", "learn")
    return False


def _repo(payload: dict) -> "Path | None":
    for env in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
        val = os.environ.get(env, "").strip()
        if val and Path(val).is_dir():
            return Path(val).resolve()
    cwd = str(payload.get("cwd") or "").strip()
    if cwd and Path(cwd).is_dir():
        return git_root(Path(cwd)) or Path(cwd).resolve()
    return None


def _relative(file_path: str, repo: "Path | None") -> str:
    p = Path(file_path)
    if repo is not None:
        try:
            return p.resolve().relative_to(repo).as_posix()
        except (ValueError, OSError):
            pass
    return p.name


def _reminder(sid: "SessionId | None", rules: list) -> "str | None":
    """The rule list, once per session. None when already shown or no session."""
    if sid is None:
        return None
    flag = sid.flag_path(REMINDER_FLAG)
    if flag.exists():
        return None
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("1", encoding="utf-8")
    except OSError:
        return None
    lines = [
        "Prose rules for notes and documents (checked on every markdown write and every "
        f"scribe add/learn; see {gate.STANDARD_DOC}):"
    ]
    lines.extend(engine.summary_lines(rules))
    lines.append(f"Check a file yourself: {CHECK_COMMAND}")
    lines.append(gate.READ_REMINDER)
    return context_block("prose", *lines)


def findings_text(rel: str, findings: list) -> str:
    lines = [f"{rel}: {engine.counts_line(findings)}"]
    for f in findings[:MAX_FINDINGS_IN_CONTEXT]:
        lines.append(f"- line {f.line} [{f.level}] {f.rule}: {f.message}")
        lines.append(f"    {f.context}")
    if len(findings) > MAX_FINDINGS_IN_CONTEXT:
        rest = len(findings) - MAX_FINDINGS_IN_CONTEXT
        lines.append(f"... and {rest} more: run {CHECK_COMMAND.replace('<file>', rel)}")
    return "\n".join(lines)


def run(payload: dict) -> "HookResult | None":
    if os.environ.get("APIARY_RUNNER_SUBPROCESS") == "1":
        return None
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    repo = _repo(payload)
    config = engine.load_config(repo)
    rules = engine.load_rules(config)
    sid = _session(payload)

    if tool_name == "Bash":
        if not is_note_write(str(tool_input.get("command") or "")):
            return None
        reminder = _reminder(sid, rules)
        return HookResult(context=reminder) if reminder else None

    if tool_name not in ("Write", "Edit"):
        return None
    file_path = str(tool_input.get("file_path") or "")
    if not file_path.lower().endswith(MARKDOWN_SUFFIXES):
        return None
    rel = _relative(file_path, repo)
    if not engine.path_included(rel, config):
        return None

    contexts: list[str] = []
    reminder = _reminder(sid, rules)
    if reminder:
        contexts.append(reminder)

    text = tool_input.get("content") if tool_name == "Write" else tool_input.get("new_string")
    if isinstance(text, str) and text.strip():
        findings = engine.filter_level(
            engine.check_text(text, path=rel, config=config, rules=rules), "warning"
        )
        if findings:
            body = findings_text(rel, findings)
            if engine.has_level(findings, "error") and gate.blocking_enabled():
                return HookResult(
                    block_reason=(
                        f"[prose] {body}\nFix the error-level finding(s) and write again "
                        f"({gate.STANDARD_DOC}). {gate.READ_REMINDER}"
                    )
                )
            contexts.append(context_block("prose", body))

    if not contexts:
        return None
    return HookResult(context="\n\n".join(contexts))


if __name__ == "__main__":
    run_standalone(run)
