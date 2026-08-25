#!/usr/bin/env python3
"""Incubator CLI — spawn a new side-project repo wired up with the apiary toolkit.

The ``spawn`` subcommand creates a fresh git repo at a target path, lays down a
Python+poetry skeleton (``pyproject.toml``, ``.gitignore``, ``CLAUDE.md``), and
migrates a ``/refine``-produced spec note from claude-apiary's scribe into the
new repo's scribe.

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
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Make the apiary repo root importable so ``from core import install`` works
# when this CLI runs under the per-repo launcher (mirrors scribe/notes.py:33).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import install as core_install
from core.utils import state
from scripts import install_git_hooks

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_SPEC_NOT_FOUND = 3
EXIT_SPAWN_FAILED = 4
EXIT_MIGRATION_FAILED = 5
EXIT_VERIFY_FAILED = 6

INCUBATOR_DIR = Path(__file__).resolve().parent
APIARY_REPO = INCUBATOR_DIR.parent
TEMPLATES_DIR = INCUBATOR_DIR / "templates"
LAUNCHER = APIARY_REPO / ".claude" / "apiary" / "launch.py"


def _run_scribe(
    args: list[str], cwd: Path, launcher: Path | None = None
) -> subprocess.CompletedProcess:
    """Invoke scribe via a launcher with a fixed cwd.

    ``launcher`` defaults to apiary's own launcher (``LAUNCHER``); pass a
    specific repo's ``.claude/apiary/launch.py`` to target that repo's scribe
    store. Resolved at call time so the ``LAUNCHER`` global stays patchable.
    """
    cmd = [sys.executable, str(launcher or LAUNCHER), "scribe/notes.py", *args]
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
    """Write .gitignore, pyproject.toml, and CLAUDE.md."""
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


def verify_spawn(target: Path) -> list[tuple[str, bool, str]]:
    """Check that a spawn actually produced a working repo (#T-2026-254).

    Returns ``[(label, passed, detail), ...]``.

    This exists because the failure mode was never a code bug. "Hexworld
    Rebuilt" was believed to be incubator-created, but the CLI had never run:
    a session read the skill's intent and hand-authored the files it describes
    instead of executing it. No .git, no launcher, no registry entry — and
    nothing surfaced the gap for a month. Prose in a skill can be paraphrased
    into a no-op; a command that inspects the filesystem cannot.
    """
    checks: list[tuple[str, bool, str]] = []

    git_dir = target / ".git"
    checks.append(("git repo", git_dir.exists(), str(git_dir)))

    launcher = target / ".claude" / "apiary" / "launch.py"
    checks.append(("apiary launcher", launcher.is_file(), str(launcher)))

    for name in ("pyproject.toml", "CLAUDE.md", ".gitignore"):
        path = target / name
        checks.append((name, path.is_file(), str(path)))

    # Registered: main-apiary's registry has an entry whose real_path is this
    # repo. Deliberately NOT find_state_dir() — that reads <target>/.apiary/
    # pointer, and a spawned repo carries no local .apiary/ dir at all
    # (55ae7ba), so it would report every healthy repo as unregistered.
    try:
        registry = json.loads(
            state.registry_path(APIARY_REPO).read_text(encoding="utf-8")
        )
        entry = state._find_entry_by_path(registry, target)
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        entry, detail = None, f"registry unreadable: {exc}"
    else:
        detail = f"uid={entry[0]}" if entry else "no registry entry for this path"
    checks.append(("registered with apiary", entry is not None, detail))

    # Commit-time secret scan (#T-2026-253 AC6). The incubator installs this on
    # spawn; verifying it here is what makes that claim checkable.
    try:
        from scripts import install_git_hooks as _igh

        hook = _igh.hook_path(target)
        installed = _igh._classify(hook) == "ours"
        hook_detail = str(hook)
    except Exception as exc:  # noqa: BLE001
        installed, hook_detail = False, f"check failed: {exc}"
    checks.append(("secret-scan pre-commit hook", installed, hook_detail))

    return checks


def _report_verify(target: Path, checks: list[tuple[str, bool, str]]) -> bool:
    """Print the check table. Returns True when everything passed."""
    failed = [c for c in checks if not c[1]]
    print(f"verify: {target}")
    for label, passed, detail in checks:
        mark = "ok  " if passed else "MISS"
        print(f"  [{mark}] {label:<28} {detail}")
    if failed:
        missing = {label for label, _, _ in failed}
        print()
        print(f"  {len(failed)} check(s) failed — this target is NOT a complete spawn.")
        # Only suggest steps that address what actually failed. A blanket
        # recipe telling you to `git init` a repo that already has .git reads
        # as boilerplate, and boilerplate gets skimmed past.
        hints = []
        if "git repo" in missing:
            hints.append(f"git init {target}")
        if {"apiary launcher", "registered with apiary"} & missing:
            hints.append(f"poetry run apiary install --target {target}")
        if "secret-scan pre-commit hook" in missing:
            hints.append(
                "python .claude/apiary/launch.py scripts/install_git_hooks.py"
                "   (run from inside the repo)"
            )
        skeleton = missing & {"pyproject.toml", "CLAUDE.md", ".gitignore"}
        if skeleton:
            hints.append(
                f"write the missing skeleton file(s) by hand: {', '.join(sorted(skeleton))}"
                "   (spawn only creates these for a NEW directory; it will not"
                " adopt an existing one)"
            )
        if hints:
            print("  Recover with:")
            for hint in hints:
                print(f"    {hint}")
        if {"git repo", "apiary launcher", "registered with apiary"} & missing:
            print()
            print("  Missing all three of .git / launcher / registration usually means")
            print("  the spawn CLI never ran, even if /incubator reported success.")
    return not failed


def cmd_verify(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser()
    if not target.is_dir():
        print(f"verify: no such directory: {target}", file=sys.stderr)
        return EXIT_VALIDATION
    ok = _report_verify(target, verify_spawn(target))
    return EXIT_OK if ok else EXIT_VERIFY_FAILED


def _install_secret_scan_hook(target: Path) -> tuple[bool, str]:
    """Install the commit-time secret-scan pre-commit hook into the new repo.

    Best-effort: a spawned repo without the hook is still a usable repo, so a
    failure here warns rather than failing the spawn. It is reported either
    way, because a hook the operator *thinks* is installed but isn't is the
    worst outcome — the whole point is that it can't be forgotten.
    """
    try:
        rc = install_git_hooks.install(target)
    except Exception as exc:  # noqa: BLE001 - never crash a spawn over a hook
        return False, f"unexpected {exc.__class__.__name__}: {exc}"
    return rc == 0, "installed" if rc == 0 else "installer refused (see output above)"


def _migrate_spec(target: Path, spec_content: str, spec_note_id: str,
                  session_id: str | None) -> tuple[bool, str]:
    """Add the spec to the new repo's scribe, then close the original in apiary.

    The ``add`` runs through the **new repo's** launcher so the spec lands in the
    new repo's store; the ``done`` runs through apiary's launcher (default) so the
    original closes in apiary's store. The new launcher exists only because
    ``cmd_spawn`` installs apiary into the target before calling this.
    """
    new_launcher = target / ".claude" / "apiary" / "launch.py"
    add_args = ["add", "--type", "context", "--content", spec_content]
    if session_id:
        add_args.extend(["--session-id", session_id])
    add_result = _run_scribe(add_args, cwd=target, launcher=new_launcher)
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

    # Bootstrap the new repo (install apiary tooling) BEFORE migrating the spec.
    # Migration writes into the new repo's store via ITS launcher, which install
    # creates — so install is a hard precondition, not best-effort. On failure
    # the repo is left intact for manual recovery and migration is skipped.
    try:
        install_result = core_install.install(target, apiary_repo=APIARY_REPO)
    except core_install.InstallError as exc:
        _report_bootstrap_failure(target, str(exc))
        return EXIT_MIGRATION_FAILED
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the CLI
        _report_bootstrap_failure(target, f"unexpected {exc.__class__.__name__}: {exc}")
        return EXIT_MIGRATION_FAILED

    hook_ok, hook_msg = _install_secret_scan_hook(target)

    migrated, msg = _migrate_spec(target, spec_content, args.spec_note_id, args.session_id)
    if not migrated:
        print(
            f"warning: spawn succeeded at {target} but spec migration failed.",
            file=sys.stderr,
        )
        print(f"  detail: {msg}", file=sys.stderr)
        print(
            f"  recover: cd into {target} and re-run "
            f"`python .claude/apiary/launch.py scribe/notes.py add --type context "
            f"--content <spec-body>` (lands the spec under "
            f"<apiary>/.repos/<name>-<id>/scribe/), then close the original via "
            f"apiary's launcher: `scribe/notes.py done {args.spec_note_id}`.",
            file=sys.stderr,
        )
        return EXIT_MIGRATION_FAILED

    # Assert the spawn actually produced what it claims before reporting
    # success (#T-2026-254). Cheap, and it turns "the CLI printed spawned:"
    # into evidence rather than a promise.
    checks = verify_spawn(target)
    if not all(passed for _, passed, _ in checks):
        print(file=sys.stderr)
        print("spawn reported success but verification failed:", file=sys.stderr)
        _report_verify(target, checks)
        return EXIT_VERIFY_FAILED

    print(f"spawned: {target}")
    print(f"  slug:    {project_slug}")
    print(f"  author:  {author}")
    print(f"  bootstrap: per-repo install OK "
          f"(uid={install_result.uid} slug={install_result.slug})")
    if hook_ok:
        print("  git hooks: secret-scan pre-commit installed")
    else:
        print(f"  git hooks: WARNING - secret-scan pre-commit NOT installed ({hook_msg});"
              " retrofit with `python .claude/apiary/launch.py scripts/install_git_hooks.py`")
    print(f"  spec:    migrated from apiary {args.spec_note_id} -> new repo scribe")
    print("  verify:  all post-spawn checks passed")
    print()
    print("next steps:")
    print(f"  cd {target}")
    print("  poetry install")
    print("  python .claude/apiary/launch.py scribe/notes.py list --type context")
    return EXIT_OK


def _report_bootstrap_failure(target: Path, detail: str) -> None:
    """Print the per-repo install failure + recovery hint to stderr."""
    print(
        f"warning: spawn created the repo at {target} but per-repo apiary "
        f"install failed; spec NOT migrated.",
        file=sys.stderr,
    )
    print(f"  detail: {detail}", file=sys.stderr)
    print(
        f"  recover: fix the install issue, then re-run `incubator spawn` "
        f"(remove {target} first) or install manually with "
        f"`apiary install --target {target}` and migrate the spec by hand.",
        file=sys.stderr,
    )


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
    p_verify = subparsers.add_parser(
        "verify",
        help="Check that a target path is a complete, working spawn",
    )
    p_verify.add_argument("--path", required=True, help="Target directory to verify")

    return parser


COMMANDS = {
    "spawn": cmd_spawn,
    "verify": cmd_verify,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
