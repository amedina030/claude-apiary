#!/usr/bin/env python3
"""
Setup script for claude-apiary.

Installs all tools (budgeter hooks, scribe commands, etc.) into the Claude Code
environment. Safe to re-run — existing claude-apiary entries are replaced, not duplicated.

Usage:
    # Global install (hooks fire in every Claude Code session):
    python setup.py --global

    # Per-project install (budgeter hooks only, scoped to one project):
    python setup.py --project-path /path/to/your/project
"""
import sys
import json
import shutil
import hashlib
import argparse
from pathlib import Path

APIS_DIR = Path(__file__).parent.resolve()
BUDGETER_DIR = APIS_DIR / "budgeter"
SCRIBE_DIR = APIS_DIR / "scribe"
DOCS_DIR = APIS_DIR / "docs"
REFINER_DIR = APIS_DIR / "refiner"
HARDEN_DIR = APIS_DIR / "harden"
COMPASS_DIR = APIS_DIR / "compass"
RESEARCHER_DIR = APIS_DIR / "researcher"
RUNNER_DIR = APIS_DIR / "runner"
INCUBATOR_DIR = APIS_DIR / "incubator"

sys.path.insert(0, str(APIS_DIR))
from core.hooks_lib import (
    APIARY_MARKER,
    APIARY_PATH_SUBSTRINGS,
    hook_cmd,
    is_apiary_entry,
    load_settings,
    register_hooks,
    save_settings,
    to_bash_path,
)
from core.utils.apiary_pointer import (
    DEFAULT_POINTER_PATH,
    get_repo_path as read_pointer_repo_path,
    read_pointer as read_apiary_pointer,
    write_pointer as write_apiary_pointer,
)

PYTHON = Path(sys.executable)

# Historical aliases kept so existing tests (core/test_setup_check.py)
# and any out-of-tree imports continue to work. The canonical definitions
# live in core.hooks_lib alongside the install/uninstall helpers.
MARKER = APIARY_MARKER
_APIARY_PATH_SUBSTRINGS = APIARY_PATH_SUBSTRINGS
_is_apiary_entry = is_apiary_entry


def _expand_hook_script_path(script_path: str) -> Path:
    """Expand a hook command's script-path string to an actual filesystem
    path that can be checked with `.exists()`.

    Handles four forms in order:
      - Relative path (from launcher format, e.g. ``core/hooks/foo.py``)
        → APIS_DIR/core/hooks/foo.py.
      - $CLAUDE_PROJECT_DIR/foo → APIS_DIR/foo (the canonical apiary repo
        root, which is what Claude Code itself sets at hook-fire time).
      - $VAR/foo or %VAR%/foo → standard env-var expansion via os.path.
      - /c/Users/... → C:/Users/... (bash-style absolute paths from
        `setup.py --global` on Windows).
    """
    # Launcher format: relative path resolved against repo root.
    if not script_path.startswith(("/", "$", "%")):
        candidate = APIS_DIR / script_path
        if candidate.exists():
            return candidate
    # Apiary-specific: $CLAUDE_PROJECT_DIR resolves to the repo root.
    expanded = script_path.replace("$CLAUDE_PROJECT_DIR", str(APIS_DIR))
    # Then standard env vars and ~.
    import os
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    p = Path(expanded)
    if p.exists():
        return p
    # Fallback: bash-style /c/Users/... → C:/Users/...
    import re as _re
    win = _re.sub(r"^/([a-z])/", lambda m: m.group(1).upper() + ":/", expanded)
    return Path(win)


def build_budgeter_hooks(*, use_launcher: bool = False):
    """Build PreToolUse, PostToolUse, and Stop hook entries for the budgeter."""
    try:
        with open(BUDGETER_DIR / "config.json", encoding="utf-8") as f:
            tools = json.load(f).get("monitored_tools", ["Agent", "Bash"])
    except (FileNotFoundError, json.JSONDecodeError):
        tools = ["Agent", "Bash"]

    root = APIS_DIR if use_launcher else None
    pre_cmd = hook_cmd(BUDGETER_DIR / "hooks" / "pre_tool_use.py", PYTHON, repo_root=root)
    post_cmd = hook_cmd(BUDGETER_DIR / "hooks" / "post_tool_use.py", PYTHON, repo_root=root)
    stop_cmd = hook_cmd(BUDGETER_DIR / "hooks" / "stop_session.py", PYTHON, repo_root=root)

    return {
        "PreToolUse": [
            {"matcher": tool, "hooks": [{"type": "command", "command": pre_cmd}]}
            for tool in tools
        ],
        "PostToolUse": [
            {"matcher": tool, "hooks": [{"type": "command", "command": post_cmd}]}
            for tool in tools
        ],
        "Stop": [
            {"hooks": [{"type": "command", "command": stop_cmd}]}
        ],
    }


CORE_DIR = APIS_DIR / "core"


def file_hash(path):
    """Return SHA-256 hex digest of a file's contents, read in 64 KiB chunks."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def build_core_hooks(*, use_launcher: bool = False):
    """Build UserPromptSubmit, PreToolUse, PostToolUse, and Stop hook entries for core hooks."""
    root = APIS_DIR if use_launcher else None
    prompt_startup_cmd = hook_cmd(CORE_DIR / "hooks" / "startup_prompt_hook.py", PYTHON, repo_root=root)
    install_cmd = hook_cmd(CORE_DIR / "hooks" / "check_install.py", PYTHON, repo_root=root)
    session_cmd = hook_cmd(CORE_DIR / "hooks" / "inject_session.py", PYTHON, repo_root=root)
    startup_cmd = hook_cmd(CORE_DIR / "hooks" / "startup_hook.py", PYTHON, repo_root=root)
    stop_cmd = hook_cmd(CORE_DIR / "hooks" / "check_install_stop.py", PYTHON, repo_root=root)
    error_reminder_cmd = hook_cmd(CORE_DIR / "hooks" / "context_rule_error_reminder.py", PYTHON, repo_root=root)
    learnings_inject_cmd = hook_cmd(CORE_DIR / "hooks" / "learnings_inject_hook.py", PYTHON, repo_root=root)
    return {
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": prompt_startup_cmd}]},
        ],
        "PreToolUse": [
            {"matcher": "", "hooks": [{"type": "command", "command": install_cmd}]},
            {"matcher": "", "hooks": [{"type": "command", "command": session_cmd}]},
            {"matcher": "", "hooks": [{"type": "command", "command": startup_cmd}]},
            {"matcher": "Edit", "hooks": [{"type": "command", "command": learnings_inject_cmd}]},
            {"matcher": "Write", "hooks": [{"type": "command", "command": learnings_inject_cmd}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": learnings_inject_cmd}]},
        ],
        "PostToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": error_reminder_cmd}]},
        ],
        "Stop": [
            {"hooks": [{"type": "command", "command": stop_cmd}]}
        ],
    }


def build_scribe_hooks(*, use_launcher: bool = False):
    """Build Stop hook entry for the scribe transcript saver."""
    root = APIS_DIR if use_launcher else None
    save_cmd = hook_cmd(CORE_DIR / "hooks" / "save_transcript.py", PYTHON, repo_root=root)
    return {
        "Stop": [
            {"hooks": [{"type": "command", "command": save_cmd}]},
        ],
    }


def build_docs_hooks(*, use_launcher: bool = False):
    """Build PreToolUse hook entry for the docs standards reminder."""
    root = APIS_DIR if use_launcher else None
    remind_cmd = hook_cmd(DOCS_DIR / "hooks" / "remind_standards.py", PYTHON, repo_root=root)
    return {
        "PreToolUse": [
            {"matcher": "", "hooks": [{"type": "command", "command": remind_cmd}]},
        ],
    }


def install_pre_commit_hook():
    """Install git pre-commit hook that runs docs/check.py."""
    git_hooks_dir = APIS_DIR / ".git" / "hooks"
    if not git_hooks_dir.is_dir():
        print(f"  Pre-commit hook  : skipped (.git/hooks/ not found)")
        return

    target = git_hooks_dir / "pre-commit"
    source = DOCS_DIR / "hooks" / "pre-commit"

    if target.exists():
        # Check if it's ours (contains docs/check.py reference)
        content = target.read_text(encoding="utf-8")
        if "docs/check.py" not in content:
            print(f"  Pre-commit hook  : WARNING — {target} already exists (not ours), skipping")
            return

    shutil.copy2(source, target)
    # Make executable (no-op on Windows, needed on Unix)
    target.chmod(target.stat().st_mode | 0o755)
    print(f"  Pre-commit hook  : {target}")


def install_post_merge_hook():
    """Install git post-merge hook that closes the scribe TODO linked to
    a merged runner branch."""
    git_hooks_dir = APIS_DIR / ".git" / "hooks"
    if not git_hooks_dir.is_dir():
        print(f"  Post-merge hook  : skipped (.git/hooks/ not found)")
        return

    target = git_hooks_dir / "post-merge"
    source = APIS_DIR / "runner" / "hooks" / "post-merge"

    if not source.exists():
        print(f"  Post-merge hook  : skipped (source not found: {source})")
        return

    if target.exists():
        content = target.read_text(encoding="utf-8")
        if "runner.close_source_todo" not in content:
            print(f"  Post-merge hook  : WARNING — {target} already exists (not ours), skipping")
            return

    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | 0o755)
    print(f"  Post-merge hook  : {target}")


def write_manifest(claude_dir: Path):
    """Write .install-manifest.json with hashes of all installed files."""
    installed_files = []

    # Add all command files
    for cmd_dir in [BUDGETER_DIR / "commands", SCRIBE_DIR / "commands", CORE_DIR / "commands", DOCS_DIR / "commands", REFINER_DIR / "commands", HARDEN_DIR / "commands", COMPASS_DIR / "commands", RESEARCHER_DIR / "commands", RUNNER_DIR / "commands", INCUBATOR_DIR / "commands"]:
        if cmd_dir.is_dir():
            for cmd_file in cmd_dir.glob("*.md"):
                installed_files.append({
                    "label": f"command: /{cmd_file.stem}",
                    "installed_path": str(claude_dir / "commands" / cmd_file.name),
                    "source_path": str(cmd_file),
                })

    manifest = {"files": []}
    for entry in installed_files:
        installed = Path(entry["installed_path"])
        if installed.exists():
            manifest["files"].append({
                "label": entry["label"],
                "installed_path": entry["installed_path"],
                "source_path": entry["source_path"],
                "hash": file_hash(installed),
            })

    manifest_path = claude_dir / ".install-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"  Manifest         : {manifest_path} ({len(manifest['files'])} files tracked)")


def check_claude_md(claude_dir: Path):
    """Check that ~/.claude/CLAUDE.md exists."""
    claude_md = claude_dir / "CLAUDE.md"

    if claude_md.exists():
        print(f"  CLAUDE.md        : {claude_md} OK")
    else:
        print(f"  CLAUDE.md        : {claude_md} not found (create manually)")


def install_commands(claude_dir: Path):
    """Copy budgeter, scribe, and core command files into ~/.claude/commands/."""
    commands_dir = claude_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    # Budgeter commands
    for cmd_file in (BUDGETER_DIR / "commands").glob("*.md"):
        shutil.copy2(cmd_file, commands_dir / cmd_file.name)

    # Scribe commands
    for cmd_file in (SCRIBE_DIR / "commands").glob("*.md"):
        shutil.copy2(cmd_file, commands_dir / cmd_file.name)

    # Core commands
    for cmd_file in (CORE_DIR / "commands").glob("*.md"):
        shutil.copy2(cmd_file, commands_dir / cmd_file.name)

    print(f"  Commands         : {commands_dir}")


def _enumerate_installed_files(claude_dir):
    """Return list of (source_path: Path, installed_path: Path, label: str)
    tuples for every file that setup.py installs into claude_dir.

    Scope: command files only.

    Exclusions (each has a dedicated check section in run_check()):
      - Hook launcher  → [Hook launcher] section already does hash comparison.
      - Git hooks      → [Pre-commit] section handles them; their installed
                         paths live under APIS_DIR/.git/, not under claude_dir,
                         making them environment-sensitive in tests.

    Deduplication: when multiple command dirs ship a file with the same name
    the last writer wins during install (matching install_commands copy order).
    We mirror that here by tracking by filename and letting later dirs overwrite
    earlier entries.
    """
    commands_dir = claude_dir / 'commands'
    # Use a dict keyed by filename so duplicate names collapse to the last
    # (winning) source, mirroring the overwrite behaviour in install_commands.
    seen: dict[str, tuple] = {}
    for cmd_dir in [
        BUDGETER_DIR / 'commands',
        SCRIBE_DIR / 'commands',
        CORE_DIR / 'commands',
        DOCS_DIR / 'commands',
        REFINER_DIR / 'commands',
        HARDEN_DIR / 'commands',
        COMPASS_DIR / 'commands',
        RESEARCHER_DIR / 'commands',
        RUNNER_DIR / 'commands',
        INCUBATOR_DIR / 'commands',
    ]:
        if cmd_dir.is_dir():
            for cmd_file in cmd_dir.glob('*.md'):
                seen[cmd_file.name] = (
                    cmd_file,
                    commands_dir / cmd_file.name,
                    f'command: /{cmd_file.stem}',
                )
    return list(seen.values())


def run_check():
    """Validate that the installation is correct and all files are in place."""
    claude_dir = Path.home() / ".claude"
    settings_path = claude_dir / "settings.json"
    ok_count = 0
    fail_count = 0

    def ok(msg):
        nonlocal ok_count
        ok_count += 1
        print(f"  OK   {msg}")

    def fail(msg):
        nonlocal fail_count
        fail_count += 1
        print(f"  FAIL {msg}")

    def check_file(path, label):
        if path.exists():
            ok(f"{label}: {path}")
        else:
            fail(f"{label}: {path} not found")

    print("INSTALL HEALTH CHECK")
    print("=" * 52)
    print()

    # 1. Settings and hooks
    print("[Hooks]")
    if not settings_path.exists():
        fail(f"Settings file: {settings_path} not found")
    else:
        ok(f"Settings file: {settings_path}")
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = settings.get("hooks", {})

        for event in ["PreToolUse", "PostToolUse", "Stop"]:
            entries = hooks.get(event, [])
            apis_entries = [e for e in entries if _is_apiary_entry(e)]
            if apis_entries:
                ok(f"{event} hooks: {len(apis_entries)} registered")
            else:
                fail(f"{event} hooks: none found for {MARKER}")

        # Check that hook commands reference existing scripts
        all_cmds = []
        for entries in hooks.values():
            for entry in entries:
                if not _is_apiary_entry(entry):
                    continue
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    if cmd:
                        all_cmds.append(cmd)

        for cmd in all_cmds:
            parts = cmd.split()
            if len(parts) < 2:
                continue
            script_path = parts[-1]
            sp = _expand_hook_script_path(script_path)
            if sp.exists():
                ok(f"Hook script: {sp.name}")
            else:
                fail(f"Hook script: {script_path} not found")
    print()

    # 2. Budgeter
    print("[Budgeter]")
    check_file(BUDGETER_DIR / "config.json", "Config")
    if (BUDGETER_DIR / "config.json").exists():
        try:
            config = json.loads((BUDGETER_DIR / "config.json").read_text(encoding="utf-8"))
            required_keys = ["monitored_tools", "rule_weights", "warn_score_threshold"]
            missing = [k for k in required_keys if k not in config]
            if missing:
                fail(f"Config missing keys: {', '.join(missing)}")
            else:
                ok(f"Config keys: all required keys present")
        except json.JSONDecodeError:
            fail("Config: invalid JSON")

    check_file(BUDGETER_DIR / "hooks" / "pre_tool_use.py", "PRE hook script")
    check_file(BUDGETER_DIR / "hooks" / "post_tool_use.py", "POST hook script")
    check_file(BUDGETER_DIR / "hooks" / "stop_session.py", "Stop hook script")

    data_dir = BUDGETER_DIR / "data"
    tmp_dir = BUDGETER_DIR / "tmp"
    if data_dir.is_dir():
        ok(f"Data directory: {data_dir}")
    else:
        fail(f"Data directory: {data_dir} not found")
    if tmp_dir.is_dir():
        ok(f"Tmp directory: {tmp_dir}")
    else:
        fail(f"Tmp directory: {tmp_dir} not found")

    # Flag files
    log_flag = Path.home() / ".claude" / "budgeter-log-enabled"
    warn_flag = Path.home() / ".claude" / "budgeter-warn-enabled"
    print(f"  {'ON  ' if log_flag.exists() else 'OFF '} budgeter-log: {log_flag}")
    print(f"  {'ON  ' if warn_flag.exists() else 'OFF '} budgeter-warn: {warn_flag}")
    print()

    # 3. Commands
    print("[Commands]")
    commands_dir = claude_dir / "commands"
    if commands_dir.is_dir():
        cmd_files = list(commands_dir.glob("*.md"))
        ok(f"Commands directory: {len(cmd_files)} commands installed")
        for f in sorted(cmd_files):
            print(f"       /{f.stem}")
    else:
        fail("Commands directory not found")
    print()

    # 4. Manifest / drift detection
    print("[Manifest]")
    manifest_path = claude_dir / ".install-manifest.json"
    if manifest_path.exists():
        ok(f"Manifest: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stale = []
        for entry in manifest.get("files", []):
            installed = Path(entry.get("installed_path", ""))
            expected = entry.get("hash", "")
            label = entry.get("label", "")
            if not installed.exists():
                stale.append(f"{label} (missing)")
            elif file_hash(installed) != expected:
                # Check if source changed (repo update) or installed was edited
                source = Path(entry.get("source_path", ""))
                if source.exists() and file_hash(source) != expected:
                    stale.append(f"{label} (repo updated, re-run setup)")
                else:
                    stale.append(f"{label} (locally modified)")
        if stale:
            for s in stale:
                fail(f"Drift: {s}")
        else:
            ok(f"All {len(manifest.get('files', []))} tracked files match manifest")
    else:
        fail("Manifest: not found (run setup.py --global to create)")
    print()

    # 5. Hook launcher
    print("[Hook launcher]")
    launcher_installed = claude_dir / "apiary_launch.py"
    launcher_source = CORE_DIR / "apiary_launch.py"
    if not launcher_installed.exists():
        fail(f"Launcher: {launcher_installed} not found (run setup.py --global)")
    elif not launcher_source.exists():
        fail(f"Launcher source: {launcher_source} not found in repo")
    elif file_hash(launcher_installed) != file_hash(launcher_source):
        fail(f"Launcher: {launcher_installed} differs from source (re-run setup.py --global)")
    else:
        ok(f"Launcher: {launcher_installed}")
    bootstrap_installed = claude_dir / "apiary_bootstrap.py"
    bootstrap_source = CORE_DIR / "apiary_bootstrap.py"
    if not bootstrap_installed.exists():
        fail(f"Bootstrap: {bootstrap_installed} not found (run setup.py --global)")
    elif not bootstrap_source.exists():
        fail(f"Bootstrap source: {bootstrap_source} not found in repo")
    elif file_hash(bootstrap_installed) != file_hash(bootstrap_source):
        fail(f"Bootstrap: {bootstrap_installed} differs from source (re-run setup.py --global)")
    else:
        ok(f"Bootstrap: {bootstrap_installed}")
    print()

    # 6. Apiary pointer file (todo #263 — lets hooks locate the repo)
    print("[Apiary pointer]")
    if not DEFAULT_POINTER_PATH.exists():
        fail(f"Pointer file: {DEFAULT_POINTER_PATH} not found (run setup.py --global)")
    else:
        payload = read_apiary_pointer()
        if payload is None:
            fail(f"Pointer file: {DEFAULT_POINTER_PATH} malformed")
        else:
            repo_path = read_pointer_repo_path()
            if repo_path is None:
                fail(
                    f"Pointer file: repo_path {payload.get('repo_path')!r} does "
                    f"not exist on disk"
                )
            elif repo_path.resolve() != APIS_DIR.resolve():
                fail(
                    f"Pointer file: repo_path={repo_path} does not match "
                    f"current checkout {APIS_DIR}"
                )
            else:
                ok(f"Pointer file: {DEFAULT_POINTER_PATH} -> {repo_path}")
    print()

    # 7. Pre-commit hook
    print("[Pre-commit]")
    git_hooks_dir = APIS_DIR / ".git" / "hooks"
    pre_commit = git_hooks_dir / "pre-commit"
    if not git_hooks_dir.is_dir():
        fail("Git hooks dir not found")
    elif not pre_commit.exists():
        fail(f"Pre-commit hook not installed: {pre_commit}")
    else:
        content = pre_commit.read_text(encoding="utf-8")
        if "docs/check.py" in content:
            ok(f"Pre-commit hook: {pre_commit}")
        else:
            fail(f"Pre-commit hook exists but doesn't run docs/check.py")
    print()

    # 7c. Compass
    print("[Compass]")
    check_file(COMPASS_DIR / "dimensions.json", "Dimensions config")
    if (COMPASS_DIR / "dimensions.json").exists():
        try:
            dims = json.loads((COMPASS_DIR / "dimensions.json").read_text(encoding="utf-8"))
            names = [d.get("name") for d in dims.get("dimensions", [])]
            if not names:
                fail("Dimensions config: no dimensions defined")
            elif len(names) != len(set(names)):
                fail("Dimensions config: duplicate dimension names")
            else:
                ok(f"Dimensions config: {len(names)} dimensions ({sum(1 for d in dims['dimensions'] if d.get('volatile'))} volatile)")
        except json.JSONDecodeError:
            fail("Dimensions config: invalid JSON")
    check_file(COMPASS_DIR / "synthesize.py", "Synthesizer")
    check_file(COMPASS_DIR / "backfill.py", "Backfill CLI")
    check_file(COMPASS_DIR / "observations.py", "Observations CLI")
    cron_registry = APIS_DIR / "runner" / "cron_registry.json"
    if cron_registry.exists():
        try:
            registry = json.loads(cron_registry.read_text(encoding="utf-8"))
            ids = [e.get("id") for e in registry.get("entries", [])]
            if "compass-weekly-synthesis" in ids:
                ok("Cron entry: compass-weekly-synthesis registered")
            else:
                fail("Cron entry: compass-weekly-synthesis missing from cron_registry.json")
        except json.JSONDecodeError:
            fail("cron_registry.json: invalid JSON")
    print()

    # 7b. Drift detection: compare installed files byte-for-byte against sources.
    print("[Drift]")
    for source, installed, label in _enumerate_installed_files(claude_dir):
        if not installed.exists():
            # Presence is reported in other sections; skip silently here.
            continue
        if not source.exists():
            fail(f"{label}: source missing at {source}")
            continue
        try:
            installed_hash = file_hash(installed)
            source_hash = file_hash(source)
        except OSError as e:
            fail(f"{label}: read error {e}")
            continue
        if installed_hash == source_hash:
            ok(f"{label}: {installed}")
        else:
            fail(f"{label}: drift between {installed} and {source}")
    print()

    # 8. Python
    print("[Runtime]")
    ok(f"Python: {PYTHON}")
    ok(f"claude-apiary: {APIS_DIR}")
    import os
    if os.environ.get("PYTHONUTF8") == "1":
        ok("PYTHONUTF8=1 (UTF-8 mode enabled)")
    elif sys.platform == "win32":
        fail("PYTHONUTF8 not set — run: [System.Environment]::SetEnvironmentVariable('PYTHONUTF8', '1', 'User')")
    else:
        ok("PYTHONUTF8 not needed (non-Windows)")

    # Import checks — verify key modules load without errors
    import_checks = [
        "core.session",
        "core.hook_context",
        "core.flags",
        "budgeter.lib.logger",
        "budgeter.lib.estimator",
        "scribe.notes",
        "compass.store",
        "compass.synthesize",
    ]
    import importlib
    for mod_name in import_checks:
        try:
            importlib.import_module(mod_name)
            ok(f"import {mod_name}")
        except Exception as e:
            fail(f"import {mod_name}: {e}")

    # Hook smoke test — run each hook with an empty payload to verify no crash
    import subprocess
    hook_scripts = [
        APIS_DIR / "core" / "hooks" / "inject_session.py",
        APIS_DIR / "core" / "hooks" / "check_install.py",
        APIS_DIR / "core" / "hooks" / "startup_hook.py",
        APIS_DIR / "core" / "hooks" / "startup_prompt_hook.py",
        APIS_DIR / "core" / "hooks" / "learnings_inject_hook.py",
        APIS_DIR / "budgeter" / "hooks" / "pre_tool_use.py",
        APIS_DIR / "budgeter" / "hooks" / "post_tool_use.py",
    ]
    for script in hook_scripts:
        if not script.exists():
            fail(f"hook missing: {script.name}")
            continue
        try:
            result = subprocess.run(
                [str(PYTHON), str(script)],
                input="{}",
                text=True,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                ok(f"hook {script.name}: runs clean")
            else:
                fail(f"hook {script.name}: exit code {result.returncode}")
        except subprocess.TimeoutExpired:
            fail(f"hook {script.name}: timed out")
        except Exception as e:
            fail(f"hook {script.name}: {e}")
    print()

    # Summary
    print("=" * 52)
    total = ok_count + fail_count
    if fail_count == 0:
        print(f"All {ok_count} checks passed.")
    else:
        print(f"{fail_count}/{total} checks FAILED.")
    return fail_count == 0


def main():
    parser = argparse.ArgumentParser(description="Set up claude-apiary tools.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--global",
        dest="global_install",
        action="store_true",
        help="Install globally in ~/.claude/settings.json",
    )
    group.add_argument(
        "--project-path",
        help="Install budgeter hooks for a specific project (absolute path to project root)",
    )
    group.add_argument(
        "--check",
        action="store_true",
        help="Validate the installation without making changes",
    )
    args = parser.parse_args()

    if args.check:
        success = run_check()
        sys.exit(0 if success else 1)

    if args.global_install:
        claude_dir = Path.home() / ".claude"
    else:
        claude_dir = Path(args.project_path).resolve() / ".claude"

    settings_path = claude_dir / "settings.json"

    # Merge all hooks into one dict, then register once to avoid stripping each other.
    # Global installs use the launcher so hooks work from any repo.
    use_launcher = args.global_install
    all_hooks = {}
    for hooks_dict in [
        build_budgeter_hooks(use_launcher=use_launcher),
        build_core_hooks(use_launcher=use_launcher),
        build_scribe_hooks(use_launcher=use_launcher),
        build_docs_hooks(use_launcher=use_launcher),
    ]:
        for event, entries in hooks_dict.items():
            all_hooks.setdefault(event, []).extend(entries)
    register_hooks(settings_path, all_hooks, MARKER, also_strip=["claude-budgeter"])

    # Ensure budgeter data/tmp dirs exist
    if args.global_install:
        (BUDGETER_DIR / "data").mkdir(exist_ok=True)
        (BUDGETER_DIR / "tmp").mkdir(exist_ok=True)
    else:
        project_config_path = claude_dir / "budgeter.json"
        if not project_config_path.exists():
            default_config = {
                "min_tasks": 50,
                "expensive_token_threshold": None,
                "expensive_percentile": 90,
                "similarity_top_n": 10,
                "monitored_tools": ["Agent", "Bash"],
            }
            with open(project_config_path, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=2)
        (claude_dir / "budgeter-tmp").mkdir(exist_ok=True)

    # Install commands (global only — command files live in ~/.claude)
    if args.global_install:
        # Apiary pointer file: lets hooks locate the apiary repo regardless
        # of which repo the Claude Code session was started in. Part of the
        # in-repo scribe state migration (decision #269, todo #263).
        pointer_path = write_apiary_pointer(APIS_DIR)
        print(f"  Apiary pointer   : {pointer_path} -> {APIS_DIR}")

        # Copy the hook launcher so global hooks can locate apiary from
        # any repo.  Source of truth: core/apiary_launch.py.
        launcher_src = CORE_DIR / "apiary_launch.py"
        launcher_dst = claude_dir / "apiary_launch.py"
        shutil.copy2(launcher_src, launcher_dst)
        print(f"  Hook launcher    : {launcher_dst}")

        # Copy the bootstrap CLI so target repos can apply apiary profiles
        # via `python ~/.claude/apiary_bootstrap.py --profile <name>`.
        # Source of truth: core/apiary_bootstrap.py.
        bootstrap_src = CORE_DIR / "apiary_bootstrap.py"
        bootstrap_dst = claude_dir / "apiary_bootstrap.py"
        shutil.copy2(bootstrap_src, bootstrap_dst)
        print(f"  Bootstrap CLI    : {bootstrap_dst}")

        install_commands(claude_dir)
        check_claude_md(claude_dir)

        # Docs commands
        commands_dir = claude_dir / "commands"
        for cmd_file in (DOCS_DIR / "commands").glob("*.md"):
            shutil.copy2(cmd_file, commands_dir / cmd_file.name)
        print(f"  Docs commands    : {commands_dir}")

        # Refiner commands
        for cmd_file in (REFINER_DIR / "commands").glob("*.md"):
            shutil.copy2(cmd_file, commands_dir / cmd_file.name)
        print(f"  Refiner commands : {commands_dir}")

        # Harden commands
        for cmd_file in (HARDEN_DIR / "commands").glob("*.md"):
            shutil.copy2(cmd_file, commands_dir / cmd_file.name)
        print(f"  Harden commands  : {commands_dir}")

        # Compass commands
        for cmd_file in (COMPASS_DIR / "commands").glob("*.md"):
            shutil.copy2(cmd_file, commands_dir / cmd_file.name)
        print(f"  Compass commands : {commands_dir}")

        # Researcher commands
        for cmd_file in (RESEARCHER_DIR / "commands").glob("*.md"):
            shutil.copy2(cmd_file, commands_dir / cmd_file.name)
        print(f"  Researcher cmds  : {commands_dir}")

        # Runner commands
        for cmd_file in (RUNNER_DIR / "commands").glob("*.md"):
            shutil.copy2(cmd_file, commands_dir / cmd_file.name)
        print(f"  Runner commands  : {commands_dir}")

        # Incubator commands
        for cmd_file in (INCUBATOR_DIR / "commands").glob("*.md"):
            shutil.copy2(cmd_file, commands_dir / cmd_file.name)
        print(f"  Incubator cmds   : {commands_dir}")

        # Pre-commit hook
        install_pre_commit_hook()

        # Post-merge hook (auto-closes scribe TODOs when runner branches merge)
        install_post_merge_hook()

        write_manifest(claude_dir)

    scope = "global" if args.global_install else f"project ({claude_dir.parent})"
    print(f"\n  Scope            : {scope}")
    print(f"  Hooks written to : {settings_path}")
    print(f"  Python executable: {PYTHON}")
    print(f"  claude-apiary      : {APIS_DIR}")
    if not args.global_install:
        print(f"  Project config   : {claude_dir / 'budgeter.json'}")
        print(f"  Project log      : {claude_dir / 'budgeter-log.jsonl'}")
    print("\nSetup complete. Start a new Claude Code session to activate.")


if __name__ == "__main__":
    main()
