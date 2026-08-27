#!/usr/bin/env python3
"""PostToolUse hook that injects two context-rule reminders when a Bash
tool call fails: the 'recover_from_trivial_errors' behavioral rule and the
'Errors Signal Doc Gaps' principle from docs/_framework.md.

Rationale (#225): CLAUDE.md and docs/_framework.md are loaded once at session
start. By the time a trivial bash error happens mid-session, those rules are
thousands of tokens behind in context and the model often drifts. This hook
fires at the exact moment enforcement is needed — right after a failed tool
call — and re-states the two rules so they sit directly on top of the failure.

Failure heuristics (any of):
  - tool_response.interrupted == True
  - tool_response.is_error == True
  - tool_response is a string (Claude Code's Bash error envelope)
  - non-zero exit_code / returncode field
  - stderr or stdout contains a Python traceback

The hook never blocks a tool call and never raises — it fails open on any
internal error so a buggy reminder never wedges the session.

Skipped entirely when ``APIARY_RUNNER_SUBPROCESS=1`` is set in the env —
runner stage subprocesses are one-shot workers and the behavioral-rule
nudge is just token bloat for them (#228).

**Doc-shaped failures file a todo** (review §5a-D.4). When the failure is
argparse saying it does not have the flag or subcommand the command used, or
the shell saying a path does not exist, the docs are usually where the wrong
thing was read. The hook locates the documentation line that still says it and
files a scribe todo naming ``doc:line`` — via the launcher idiom, at most once
per session per command, and deduplicated across sessions by a unique tag so a
recurring mistake does not accumulate duplicate todos. Reading the rule is a
nudge; the todo is what survives the session.
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.hook_context import HookResult, run_standalone
from core.session import SessionId

REMINDER = (
    "[context-rule: recover_from_trivial_errors] A tool call just failed. "
    "If the fix is obvious from the error message and doesn't change your "
    "plan, fix and retry in the same turn without narrating. Only surface "
    "if the error reveals a wrong assumption or needs a real user decision."
    "\n\n"
    "[docs/_framework.md: Errors Signal Doc Gaps] Every avoidable error is "
    "a signal that either (a) the docs weren't consulted, or (b) the docs "
    "don't cover this usage. After recovering, ask: could documentation "
    "have prevented this? If yes, either update the relevant doc or fix "
    "the loading/consultation pattern so the doc gets read next time. The "
    "goal is that the same category of error never happens twice."
)

TRACEBACK_MARKER = "Traceback (most recent call last):"

# Marks that indicate the failure was a hook-policy denial, not a trivial
# tool error. The behavioral rule does not apply to denials — the user (via
# a hook) explicitly blocked the call, so retrying silently would be wrong.
HOOK_DENY_MARKERS = (
    "denied this tool",
    "Hook PreToolUse",
    "permissionDecision",
    "Blocked by",
)


def _is_hook_denial(text: str) -> bool:
    return any(m in text for m in HOOK_DENY_MARKERS)


def _looks_like_failure(tool_response) -> bool:
    """Return True if the tool_response envelope indicates a real failure
    (not success, not a hook-policy denial).
    """
    # String-form response: Claude Code sometimes returns a bare error string.
    if isinstance(tool_response, str):
        if not tool_response:
            return False
        if _is_hook_denial(tool_response):
            return False
        return TRACEBACK_MARKER in tool_response or "error" in tool_response.lower()

    if not isinstance(tool_response, dict):
        return False

    # Explicit failure flags from the tool envelope.
    if tool_response.get("interrupted") is True:
        return True
    if tool_response.get("is_error") is True or tool_response.get("isError") is True:
        return True

    # Some tool envelopes carry a numeric exit code.
    for key in ("exit_code", "exitCode", "returncode", "return_code"):
        code = tool_response.get(key)
        if isinstance(code, int) and code != 0:
            return True

    # Fall back to scanning stdout/stderr for a traceback. Skip if the text
    # is actually a hook denial — those are not trivial errors.
    combined = ""
    for key in ("stdout", "stderr", "output", "content", "error"):
        v = tool_response.get(key)
        if isinstance(v, str):
            combined += v + "\n"
    if combined and _is_hook_denial(combined):
        return False
    if TRACEBACK_MARKER in combined:
        return True

    return False


# --------------------------------------------------------------------------- #
# Doc-shaped failures (review §5a-D.4)
# --------------------------------------------------------------------------- #

#: What argparse and the shell say when the docs were wrong. Each pattern
#: captures the token that does not exist.
DOC_SHAPED = (
    re.compile(r"unrecognized arguments:\s*(\S+)"),
    re.compile(r"error: argument [^:]*: invalid choice:\s*'([^']+)'"),
    re.compile(r"no such option:\s*(\S+)"),
    re.compile(r"can't open file '([^']+)'"),
    re.compile(r"No such file or directory:?\s*'?([^\s'\"]+)'?"),
)

#: Where a documented command could have come from.
DOC_GLOBS = ("docs/**/*.md", "*/commands/*.md", "*/CLAUDE.md")
DOC_FILES = ("README.md", "SETUP.md", "PORTABILITY.md", "RELEASING.md", "CLAUDE.md")

#: Never blamed: the review appendices are dated snapshots by design.
DOC_SKIP_PARTS = {"review"}

SCRIBE_TIMEOUT = 20  # seconds; the todo is best-effort, never worth a hang


def _target_repo() -> Path:
    """The repo this session is working in."""
    for env in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
        val = os.environ.get(env, "").strip()
        if val and Path(val).is_dir():
            return Path(val)
    return REPO_ROOT


def offending_token(text: str) -> str | None:
    """The flag, subcommand or path the failure says does not exist."""
    for pattern in DOC_SHAPED:
        m = pattern.search(text)
        if m and m.group(1).strip():
            return m.group(1).strip().strip("'\"")
    return None


def _iter_docs(repo: Path):
    for name in DOC_FILES:
        p = repo / name
        if p.is_file():
            yield p
    for pattern in DOC_GLOBS:
        for p in repo.glob(pattern):
            if p.is_file() and not DOC_SKIP_PARTS & set(p.relative_to(repo).parts):
                yield p


def find_doc_line(token: str, repo: Path | None = None) -> str | None:
    """``path:line`` of the first documentation line that still names *token*.

    A flag or subcommand is matched at a word boundary, so ``--dry`` does not
    find the line documenting ``--dry-run`` and blame the wrong claim. A path
    is matched as a substring, because docs wrap paths in backticks, quotes
    and table cells. Returns None when nothing documents the token — in which
    case the failure was a typo, not a doc gap, and no todo is filed.
    """
    repo = repo or REPO_ROOT
    if not token:
        return None
    needle = token.replace("\\", "/")
    matcher = None
    if re.fullmatch(r"-{0,2}[A-Za-z][\w-]*", needle):
        matcher = re.compile(rf"(?<![\w-]){re.escape(needle)}(?![\w-])")
    for doc in _iter_docs(repo):
        try:
            lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, start=1):
            haystack = line.replace("\\", "/")
            hit = matcher.search(haystack) if matcher else (needle in haystack)
            if hit:
                return f"{doc.relative_to(repo).as_posix()}:{n}"
    return None


def command_of(payload: dict) -> str:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        return str(tool_input.get("command") or "")
    return ""


def response_text(tool_response) -> str:
    if isinstance(tool_response, str):
        return tool_response
    if not isinstance(tool_response, dict):
        return ""
    parts = []
    for key in ("stderr", "stdout", "output", "content", "error"):
        v = tool_response.get(key)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


def _command_key(command: str) -> str:
    """A stable, filename-safe handle for "this command shape".

    The first two argv words are enough to distinguish `scribe/notes.py add`
    from `scribe/notes.py learn` without letting a changing `--content` value
    defeat the once-per-session guard.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    words = [w for w in words if not w.startswith("-")][:3]
    handle = "-".join(Path(w).name for w in words) or "command"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", handle)[:60]


def _already_filed(session_id: str, key: str) -> bool:
    """Once per session per command shape. Failure to check means "not yet"."""
    try:
        flag = SessionId(session_id).flag_path(f"doc_todo_{key}")
    except (ValueError, OSError):
        return True  # no session: do not file, rather than file blindly
    if flag.exists():
        return True
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("1", encoding="utf-8")
    except OSError:
        return True
    return False


def file_doc_todo(repo: Path, token: str, doc_ref: str, command: str) -> bool:
    """File the scribe todo through the repo's launcher. Never raises.

    Deduplicated across sessions by ``--unique-tag``: scribe skips the add when
    an active note already carries that tag, so a mistake repeated next week
    updates nothing rather than piling up.
    """
    launcher = repo / ".claude" / "apiary" / "launch.py"
    if not launcher.is_file():
        return False
    doc, _, line = doc_ref.partition(":")
    tag = "doc-drift:" + re.sub(r"[^A-Za-z0-9._/-]+", "-", f"{doc}:{token}")[:80]
    content = (
        f"Fix the doc that still documents `{token}`.\n\n"
        f"A documented command failed with it: `{command.strip()[:300]}`\n"
        f"The claim is at {doc}:{line}.\n\n"
        f"Either the code lost `{token}` and the doc must be corrected "
        f"(if the table is generated, re-run the generator with --write), "
        f"or the doc was right and the code regressed. Done when "
        f"`python docs/test_doc_examples.py` and "
        f"`python docs/check_cli_claims.py` both pass on the corrected doc."
    )
    argv = [
        sys.executable,
        str(launcher),
        "scribe/notes.py",
        "add",
        "--type",
        "todo",
        "--content",
        content,
        "--unique-tag",
        tag,
    ]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=SCRIBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def doc_gap(payload: dict) -> tuple[str, str] | None:
    """``(token, "doc:line")`` when this failure is a documented claim gone wrong."""
    command = command_of(payload)
    if not command:
        return None
    token = offending_token(response_text(payload.get("tool_response")))
    if not token:
        return None
    if token not in command:
        # The failure names something the command did not pass — a nested
        # process's problem, not this documented invocation's.
        return None
    doc_ref = find_doc_line(token, _target_repo())
    return (token, doc_ref) if doc_ref else None


def run(payload: dict) -> HookResult | None:
    """Return the behavioural reminder when a Bash call just failed."""
    # Runner subprocesses skip this hook entirely (#228).
    if os.environ.get("APIARY_RUNNER_SUBPROCESS") == "1":
        return None

    if payload.get("tool_name") != "Bash":
        return None

    if not _looks_like_failure(payload.get("tool_response")):
        return None

    context = REMINDER
    try:
        gap = doc_gap(payload)
    except Exception:  # noqa: BLE001 — observability must never break a session
        gap = None
    if gap is not None:
        token, doc_ref = gap
        repo = _target_repo()
        key = _command_key(command_of(payload))
        if not _already_filed(str(payload.get("session_id") or ""), key):
            try:
                filed = file_doc_todo(repo, token, doc_ref, command_of(payload))
            except Exception:  # noqa: BLE001
                filed = False
            context += (
                "\n\n[docs] That failure is doc-shaped: `" + token + "` is still "
                "documented at "
                + doc_ref
                + ". "
                + (
                    "A scribe todo naming it has been filed — "
                    if filed
                    else "Filing a todo failed, so please note it yourself — "
                )
                + "fix the doc (or re-run the generator that owns that table) "
                "rather than only working around it here."
            )
    return HookResult(context=context)


if __name__ == "__main__":
    run_standalone(run, event="PostToolUse")
