#!/usr/bin/env python3
"""Incubator CLI — spawn a new side-project repo wired up with the apiary toolkit.

The ``spawn`` subcommand creates a fresh git repo at a target path, lays down a
Python+poetry skeleton (``pyproject.toml``, ``.gitignore``, ``CLAUDE.md``, an
empty ``.apiary/`` directory), and migrates a ``/refine``-produced spec note from
claude-apiary's scribe into the new repo's scribe.

Usage:
    cli.py spawn --path <abs-path> --spec-note-id <id> [--author "<name>"]

Exit codes:
    0  success
    2  validation error (bad path, target exists, inside another repo, ...)
    3  spec note not found in apiary scribe
    4  spawn failure (git init failed, template write failed, ...)
    5  partial success (repo created, spec migration failed)
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_SPEC_NOT_FOUND = 3
EXIT_SPAWN_FAILED = 4
EXIT_MIGRATION_FAILED = 5

INCUBATOR_DIR = Path(__file__).resolve().parent
APIARY_REPO = INCUBATOR_DIR.parent
TEMPLATES_DIR = INCUBATOR_DIR / "templates"
LAUNCHER = Path.home() / ".claude" / "apiary_launch.py"


def _run_scribe(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Invoke scribe via the launcher with a fixed cwd."""
    cmd = [sys.executable, str(LAUNCHER), "scribe/notes.py", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _slugify_dirname(name: str) -> str:
    """Turn a directory basename into a PEP 508-compatible package name."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return cleaned or "project"


def _detect_author() -> str:
    """Best-effort author string from git config; falls back to a placeholder."""
    try:
        name = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        email = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = email = ""
    if name and email:
        return f"{name} <{email}>"
    if name:
        return name
    return "you <you@example.com>"


def _validate_target(path_str: str) -> tuple[Path | None, str | None]:
    """Return (path, None) if valid; (None, error_message) if not."""
    if not path_str:
        return None, "path must not be empty"
    path = Path(path_str)
    if not path.is_absolute():
        return None, f"path must be absolute: {path_str!r}"
    path = path.resolve()
    if path.exists():
        return None, f"target path already exists: {path}"
    parent = path.parent
    if not parent.is_dir():
        return None, f"parent directory does not exist: {parent}"
    inside = subprocess.run(
        ["git", "-C", str(parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if inside.returncode == 0 and inside.stdout.strip():
        return None, (
            f"parent {parent} is inside an existing git repo "
            f"({inside.stdout.strip()}); refusing to nest a new project there"
        )
    return path, None


def _fetch_spec(spec_note_id: str) -> tuple[str | None, str | None]:
    """Read a spec note from apiary's scribe. Return (content, None) or (None, error)."""
    result = _run_scribe(["get", spec_note_id], cwd=APIARY_REPO)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout or "spec note not found").strip()
    out = result.stdout
    sep = out.find("\n---\n")
    if sep == -1:
        return None, "scribe get output did not contain a '---' separator"
    return out[sep + len("\n---\n"):].rstrip("\n") + "\n", None


def _parse_goal_lines(spec: str) -> tuple[str, str, str]:
    """Pull the Problem/Solution/Value bullets out of the spec body."""
    def grab(label: str) -> str:
        pattern = rf"^- \*\*{label}:\*\*\s*(.+)$"
        match = re.search(pattern, spec, re.MULTILINE)
        return match.group(1).strip() if match else ""
    return grab("Problem"), grab("Solution"), grab("Value")


def _render_template(template_path: Path, replacements: dict[str, str]) -> str:
    text = template_path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run_git_init(target: Path) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(target),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "git init failed").strip()
    return True, ""


def _lay_down_skeleton(target: Path, project_slug: str, project_name: str,
                       author: str, spec_content: str) -> None:
    """Write .gitignore, pyproject.toml, CLAUDE.md, and create .apiary/ skeleton."""
    problem, solution, value = _parse_goal_lines(spec_content)
    description = solution or f"{project_name} side project"

    gitignore = _render_template(TEMPLATES_DIR / "gitignore.tmpl", {})
    _write_file(target / ".gitignore", gitignore)

    pyproject = _render_template(
        TEMPLATES_DIR / "pyproject.toml.tmpl",
        {
            "__PROJECT_SLUG__": project_slug,
            "__PROJECT_DESCRIPTION__": description.replace('"', "'"),
            "__PROJECT_AUTHOR__": author.replace('"', "'"),
        },
    )
    _write_file(target / "pyproject.toml", pyproject)

    claude_md = _render_template(
        TEMPLATES_DIR / "CLAUDE.md.tmpl",
        {
            "__PROJECT_NAME__": project_name,
            "__PROBLEM_LINE__": f"**Problem:** {problem}" if problem else "_Problem: see spec note._",
            "__SOLUTION_LINE__": f"**Solution:** {solution}" if solution else "_Solution: see spec note._",
            "__VALUE_LINE__": f"**Value:** {value}" if value else "_Value: see spec note._",
        },
    )
    _write_file(target / "CLAUDE.md", claude_md)

    (target / ".apiary").mkdir(parents=True, exist_ok=True)


def _migrate_spec(target: Path, spec_content: str, spec_note_id: str,
                  session_id: str | None) -> tuple[bool, str]:
    """Add the spec to the new repo's scribe, then close the original in apiary."""
    add_args = ["add", "--type", "context", "--content", spec_content]
    if session_id:
        add_args.extend(["--session-id", session_id])
    add_result = _run_scribe(add_args, cwd=target)
    if add_result.returncode != 0:
        return False, (add_result.stderr or add_result.stdout or "scribe add failed").strip()

    done_result = _run_scribe(["done", spec_note_id], cwd=APIARY_REPO)
    if done_result.returncode != 0:
        return False, (
            f"spec was added to {target} but failed to close original {spec_note_id} "
            f"in apiary: {(done_result.stderr or done_result.stdout).strip()}"
        )
    return True, add_result.stdout.strip()


def _cleanup_partial(target: Path) -> None:
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def cmd_spawn(args: argparse.Namespace) -> int:
    target, err = _validate_target(args.path)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_VALIDATION

    spec_content, spec_err = _fetch_spec(args.spec_note_id)
    if spec_err:
        print(f"error: {spec_err}", file=sys.stderr)
        return EXIT_SPEC_NOT_FOUND

    project_name = target.name
    project_slug = _slugify_dirname(project_name)
    author = args.author or _detect_author()

    target.mkdir(parents=True, exist_ok=False)
    ok, git_err = _run_git_init(target)
    if not ok:
        _cleanup_partial(target)
        print(f"error: git init failed: {git_err}", file=sys.stderr)
        return EXIT_SPAWN_FAILED

    try:
        _lay_down_skeleton(target, project_slug, project_name, author, spec_content)
    except OSError as exc:
        _cleanup_partial(target)
        print(f"error: failed to write skeleton: {exc}", file=sys.stderr)
        return EXIT_SPAWN_FAILED

    migrated, msg = _migrate_spec(target, spec_content, args.spec_note_id, args.session_id)
    if not migrated:
        print(
            f"warning: spawn succeeded at {target} but spec migration failed.",
            file=sys.stderr,
        )
        print(f"  detail: {msg}", file=sys.stderr)
        print(
            f"  recover: cd into {target} and re-run "
            f"`python ~/.claude/apiary_launch.py scribe/notes.py add --type context "
            f"--content <spec-body>` (auto-registers the new repo and lands the "
            f"spec under <apiary>/.repos/<name>-<id>/scribe/), then close the "
            f"original via `scribe/notes.py done {args.spec_note_id}`.",
            file=sys.stderr,
        )
        return EXIT_MIGRATION_FAILED

    print(f"spawned: {target}")
    print(f"  slug:    {project_slug}")
    print(f"  author:  {author}")
    print(f"  spec:    migrated from apiary {args.spec_note_id} -> new repo scribe")

    bootstrap_msg = _per_repo_bootstrap(target)
    if bootstrap_msg:
        print(f"  bootstrap: {bootstrap_msg}")
    print()
    print("next steps:")
    print(f"  cd {target}")
    print("  poetry install")
    print(
        "  python ~/.claude/apiary_launch.py scribe/notes.py list --type context"
    )
    return EXIT_OK


def _per_repo_bootstrap(target: Path) -> str:
    """Best-effort: invoke ``apiary install --target <new repo>`` so the
    new repo gets per-repo hooks / pin files alongside any global install
    the user already has. Failures here do NOT fail incubator (returns a
    short status string for the spawn report).

    Per MIGRATION-PLAN.md §13.11 + phase 6 polish — every newly-spawned
    repo should have apiary integration without the user having to run
    install separately.
    """
    try:
        from core import install as install_mod
    except ImportError as exc:
        return f"skipped (core.install not importable: {exc})"
    try:
        result = install_mod.install(target)
    except install_mod.InstallError as exc:
        return f"skipped ({exc})"
    except Exception as exc:  # noqa: BLE001 — best-effort wrapper
        return f"skipped (unexpected error: {exc.__class__.__name__})"
    return f"per-repo install OK (uid={result.uid} slug={result.slug})"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incubator",
        description="Spawn a new side-project repo wired up with apiary.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_spawn = subparsers.add_parser(
        "spawn", help="Create a new repo at a target path and migrate a refine spec into it"
    )
    p_spawn.add_argument("--path", required=True, help="Absolute target directory (must not exist)")
    p_spawn.add_argument(
        "--spec-note-id",
        required=True,
        help="ID of the /refine context note in apiary scribe (e.g. C-2026-43)",
    )
    p_spawn.add_argument(
        "--author",
        help="Author string for pyproject.toml; defaults to git config user.name <user.email>",
    )
    p_spawn.add_argument(
        "--session-id",
        help="Optional session ID stamped on the migrated spec note",
    )
    return parser


COMMANDS = {
    "spawn": cmd_spawn,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
