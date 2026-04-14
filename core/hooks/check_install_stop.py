#!/usr/bin/env python3
"""
Stop hook — placeholder. Previously cleaned up per-session flag files
written by PreToolUse hooks (check_install.py, inject_session.py,
startup_hook.py, budgeter/pre_tool_use.py), but Stop fires at the end
of *every* assistant turn — not session end — so the cleanup was
resetting "run once per session" guards and causing context reminders
to re-fire on every tool call (T-2026-117).

Session-scoped flags are keyed by session_id and safe to persist;
sessions don't come back. External maintenance can prune old flag
directories by mtime if needed.
"""
import sys


def main():
    sys.exit(0)


if __name__ == "__main__":
    main()
