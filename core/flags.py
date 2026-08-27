"""
Flag file management for claude-apiary tools.

Each toggle is a sentinel file at
``<repo>/.claude/apiary/flags/<flag_name>-enabled``. Apiary tools call
``is_enabled(name)``; the ``/budgeter`` slash command drives the
``toggle``/``enable``/``disable``/``status`` CLI at the bottom of this
module. Repo discovery order:

1. ``$CLAUDE_PROJECT_DIR`` (set by Claude Code at hook-fire time).
2. ``$APIARY_TARGET_REPO`` — explicit override for tests / CLI.
3. The git root containing cwd, when neither env var is set.

When none of those resolve to a directory, the helpers raise
``RuntimeError`` — there is no global fallback in the per-repo
install model (``docs/architecture/per-repo-install.md``).

CLI::

    python core/flags.py toggle budgeter-log     # -> "ON" / "OFF"
    python core/flags.py status budgeter-session-warn

Exit 0 on success, 1 when no bootstrapped repo is in scope or the flag
name is malformed.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# `python core/flags.py toggle <flag>` is a documented entry point (the
# /budgeter-* skills invoke it through the launcher), and Python only puts
# the script's own directory on sys.path — not the repo root the import
# below needs.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.utils.gitutil import git_root  # noqa: E402

PIN_FLAGS_SUBPATH = ".claude/apiary/flags"

# Flag names become filenames — keep them to a conservative slug so a
# caller can never walk out of the flags directory.
_FLAG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")  # \Z: `$` would accept a trailing newline


class FlagsRepoUnresolved(RuntimeError):
    """Raised when no bootstrapped repo is in scope for a flag operation."""


def _per_repo_root() -> Path | None:
    """Best-effort resolution of the bootstrapped repo. Returns None when
    no repo can be located — caller decides how to react."""
    for env in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
        val = os.environ.get(env, "").strip()
        if val and Path(val).is_dir():
            return Path(val)
    # Last-ditch: cwd's git root. Cheap enough for one git invocation per
    # process; only matters outside hooks (CLI tools).
    return git_root()


def _flag_path(flag_name: str) -> Path:
    """Return the absolute path of the flag file in the current repo.

    Raises ``FlagsRepoUnresolved`` when no bootstrapped repo can be
    located, so callers don't silently miss a misconfigured environment.
    """
    repo = _per_repo_root()
    if repo is None:
        raise FlagsRepoUnresolved(
            f"cannot resolve a bootstrapped repo for flag {flag_name!r}; "
            "set CLAUDE_PROJECT_DIR or APIARY_TARGET_REPO, or run from "
            "inside a bootstrapped repo's git tree."
        )
    return repo / PIN_FLAGS_SUBPATH / f"{flag_name}-enabled"


def is_enabled(flag_name: str) -> bool:
    """Return True iff ``<repo>/.claude/apiary/flags/<flag>-enabled`` exists."""
    try:
        return _flag_path(flag_name).is_file()
    except FlagsRepoUnresolved:
        # Hooks call this without a clear repo in some edge cases (e.g.
        # incubator spawning a fresh repo). Treat unresolvable as
        # "disabled" rather than crashing the caller.
        return False


def enable(flag_name: str) -> None:
    """Create the flag file for the current repo."""
    target = _flag_path(flag_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("enabled", encoding="utf-8")


def disable(flag_name: str) -> None:
    """Remove the flag file for the current repo if present."""
    try:
        target = _flag_path(flag_name)
    except FlagsRepoUnresolved:
        return
    if target.exists():
        target.unlink()


def toggle(flag_name: str) -> bool:
    """Toggle flag. Returns new state (True = enabled)."""
    if is_enabled(flag_name):
        disable(flag_name)
        return False
    enable(flag_name)
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

_VERBS = {
    "toggle": "Flip the flag and print its new state",
    "enable": "Create the flag file and print ON",
    "disable": "Remove the flag file and print OFF",
    "status": "Print the current state without changing it",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the ``flags.py`` argument parser (also read by docs/check_cli_claims.py)."""
    parser = argparse.ArgumentParser(
        prog="flags.py",
        description=(
            "Toggle apiary feature flags. Each flag is a sentinel file at "
            "<repo>/.claude/apiary/flags/<name>-enabled; presence means enabled."
        ),
    )
    subparsers = parser.add_subparsers(dest="verb", required=True)
    for verb, help_text in _VERBS.items():
        sub = subparsers.add_parser(verb, help=help_text, description=help_text)
        sub.add_argument(
            "name",
            help="Flag name, e.g. budgeter-log, budgeter-session-warn, auto-startup",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Prints ``ON``/``OFF``; returns 0 on success, 1 on error."""
    args = build_parser().parse_args(argv)
    name = args.name

    if not _FLAG_NAME_RE.match(name):
        print(
            f"error: invalid flag name {name!r}; expected letters, digits, "
            "'.', '_' or '-'",
            file=sys.stderr,
        )
        return 1

    try:
        # Resolve first so every verb fails the same way on a bad
        # environment — `disable` and `status` would otherwise no-op.
        _flag_path(name)
        if args.verb == "toggle":
            state = toggle(name)
        elif args.verb == "enable":
            enable(name)
            state = True
        elif args.verb == "disable":
            disable(name)
            state = False
        else:  # status
            state = is_enabled(name)
    except (FlagsRepoUnresolved, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("ON" if state else "OFF")
    return 0


if __name__ == "__main__":
    sys.exit(main())
