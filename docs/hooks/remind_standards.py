#!/usr/bin/env python3
"""
PreToolUse hook — reminds Claude to consult standards docs when writing code or docs.

Fires on Write and Edit tools. Injects a one-line reminder pointing at the
relevant standards doc, once per file category per session.

**Classification is relative to the repo the write is happening in**, not to
main-apiary's own checkout. It used to resolve every path against
``REPO_ROOT`` and compare the first path segment against a hard-coded
``{"budgeter", "scribe", "core", "docs", ".claude"}`` — so in any *other*
bootstrapped repo the ``relative_to`` raised and the hook returned ``None`` for
every file, and inside main-apiary a write to ``runner/``, ``harden/`` or
``gui/`` was classified as a brand-new tool and nudged toward a checklist it
did not need (T-2026-282).

The "new tool" signal is now derived rather than listed: a ``.py`` file whose
top-level directory contains no other Python is a directory that is becoming a
tool, in whichever repo the session is working in.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apiary root

from core.hook_context import HookResult, context_block, run_standalone
from core.session import SessionId
from core.utils.gitutil import git_root

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# Map file patterns to standards docs
STANDARDS = {
    "code": "docs/standards/code-style.md",
    "doc": "docs/standards/doc-style.md",
    "tool": "docs/standards/new-tool-checklist.md",
}


def _flag_path(session_id: str, reminder_key: str) -> Path:
    """Path to a flag file tracking whether a reminder was already shown."""
    sid = SessionId(session_id)
    return sid.flag_path(f"remind_{reminder_key}")


def _already_reminded(session_id: str, reminder_key: str) -> bool:
    """Check if this reminder was already shown this session."""
    return _flag_path(session_id, reminder_key).exists()


def _mark_reminded(session_id: str, reminder_key: str) -> None:
    """Mark a reminder as shown for this session."""
    fp = _flag_path(session_id, reminder_key)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("1", encoding="utf-8")


def _session_repo(payload: dict) -> Path | None:
    """The repo this write belongs to.

    ``CLAUDE_PROJECT_DIR`` is set by Claude Code, ``APIARY_TARGET_REPO`` by the
    per-repo launcher; the payload's own ``cwd`` is the fallback. Falls back to
    main-apiary last, so a session with no signals still behaves as before.
    """
    for env in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
        val = os.environ.get(env, "").strip()
        if val and Path(val).is_dir():
            return Path(val).resolve()
    cwd = str(payload.get("cwd") or "").strip()
    if cwd and Path(cwd).is_dir():
        return git_root(Path(cwd)) or Path(cwd).resolve()
    return REPO_ROOT


def _is_new_tool_dir(top_dir: Path, written: Path) -> bool:
    """True when *top_dir* holds no Python other than the file being written.

    That is the observable difference between "adding a file to a tool" and
    "starting a tool": the second one is the case the checklist is for.
    """
    if not top_dir.is_dir():
        return True          # the directory is being created by this write
    for existing in top_dir.rglob("*.py"):
        if existing.resolve() != written:
            return False
    return True


def _classify_file(file_path: str, repo: Path) -> str | None:
    """Which standard applies to *file_path*, relative to *repo*. Or None."""
    try:
        fp = Path(file_path).resolve()
        rel = fp.relative_to(repo)
    except (ValueError, OSError):
        return None

    rel_posix = rel.as_posix()

    if rel_posix.startswith("docs/") and fp.suffix == ".md":
        return "doc"

    if fp.suffix == ".py":
        parts = rel.parts
        if len(parts) >= 2 and _is_new_tool_dir(repo / parts[0], fp):
            return "tool"
        return "code"

    return None


def _standard_for(category: str, repo: Path) -> str:
    """The doc path to name, preferring the one in the session's own repo.

    A bootstrapped repo usually has no ``docs/standards/``; pointing at a file
    that is not there is worse than pointing at main-apiary's copy.
    """
    rel = STANDARDS[category]
    if (repo / rel).is_file():
        return rel
    return f"{REPO_ROOT.name}/{rel}"


def run(payload: dict) -> HookResult | None:
    """Return the standards reminder on the session's first write of a kind."""
    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Write", "Edit"):
        return None

    session_id = payload.get("session_id", "")
    if not session_id:
        return None

    # Extract file path from tool input
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None

    repo = _session_repo(payload)
    if repo is None:
        return None

    category = _classify_file(file_path, repo)
    if not category:
        return None

    # Check if already reminded this session
    if _already_reminded(session_id, category):
        return None

    # Mark as reminded and inject context
    _mark_reminded(session_id, category)
    reminder = (f"Standards: read {_standard_for(category, repo)} before "
                "proceeding (first write of this type this session)")
    return HookResult(context=context_block("docs", reminder))


if __name__ == "__main__":
    run_standalone(run)
