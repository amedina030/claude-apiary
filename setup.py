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

sys.path.insert(0, str(APIS_DIR))
from core.hooks_lib import to_bash_path, hook_cmd, load_settings, save_settings, register_hooks

PYTHON = Path(sys.executable)
MARKER = "claude-apiary"

# Path-suffix substrings that identify a hook entry as ours even when its
# command uses a portable $CLAUDE_PROJECT_DIR template (no absolute path
# containing the literal MARKER). Both forward- and back-slash variants
# are matched to handle Windows hand-edited entries. (#227)
_APIARY_PATH_SUBSTRINGS = (
    "/budgeter/hooks/", "\\budgeter\\hooks\\",
    "/core/hooks/", "\\core\\hooks\\",
    "/scribe/", "\\scribe\\",
    "/docs/hooks/", "\\docs\\hooks\\",
    "/refiner/", "\\refiner\\",
    "/harden/", "\\harden\\",
    "/runner/", "\\runner\\",
)


def _is_apiary_entry(entry) -> bool:
    """Return True if a settings.json hook entry was installed by apiary.

    Recognizes two formats:
      1. Absolute-path entries written by `setup.py --global` whose path
         contains the literal MARKER ("claude-apiary").
      2. Portable hand-edited entries that use `$CLAUDE_PROJECT_DIR/<sub>/...`
         and therefore lack the marker. Detected by known apiary subpath
         substrings (budgeter/hooks/, core/hooks/, scribe/, etc.).
    """
    blob = json.dumps(entry)
    if MARKER in blob:
        return True
    return any(sub in blob for sub in _APIARY_PATH_SUBSTRINGS)


def _expand_hook_script_path(script_path: str) -> Path:
    """Expand a hook command's script-path string to an actual filesystem
    path that can be checked with `.exists()`.

    Handles three forms in order:
      - $CLAUDE_PROJECT_DIR/foo → APIS_DIR/foo (the canonical apiary repo
        root, which is what Claude Code itself sets at hook-fire time).
      - $VAR/foo or %VAR%/foo → standard env-var expansion via os.path.
      - /c/Users/... → C:/Users/... (bash-style absolute paths from
        `setup.py --global` on Windows).
    """
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


def build_budgeter_hooks():
    """Build PreToolUse, PostToolUse, and Stop hook entries for the budgeter."""
    try:
        with open(BUDGETER_DIR / "config.json", encoding="utf-8") as f:
            tools = json.load(f).get("monitored_tools", ["Agent", "Bash"])
    except (FileNotFoundError, json.JSONDecodeError):
        tools = ["Agent", "Bash"]

    pre_cmd = hook_cmd(BUDGETER_DIR / "hooks" / "pre_tool_use.py", PYTHON)
    post_cmd = hook_cmd(BUDGETER_DIR / "hooks" / "post_tool_use.py", PYTHON)
    stop_cmd = hook_cmd(BUDGETER_DIR / "hooks" / "stop_session.py", PYTHON)

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
    """Return SHA-256 hex digest of a file's contents."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_core_hooks():
    """Build UserPromptSubmit, PreToolUse, PostToolUse, and Stop hook entries for core hooks."""
    prompt_startup_cmd = hook_cmd(CORE_DIR / "hooks" / "startup_prompt_hook.py", PYTHON)
    install_cmd = hook_cmd(CORE_DIR / "hooks" / "check_install.py", PYTHON)
    session_cmd = hook_cmd(CORE_DIR / "hooks" / "inject_session.py", PYTHON)
    startup_cmd = hook_cmd(CORE_DIR / "hooks" / "startup_hook.py", PYTHON)
    stop_cmd = hook_cmd(CORE_DIR / "hooks" / "check_install_stop.py", PYTHON)
    error_reminder_cmd = hook_cmd(CORE_DIR / "hooks" / "context_rule_error_reminder.py", PYTHON)
    return {
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": prompt_startup_cmd}]},
        ],
        "PreToolUse": [
            {"matcher": "", "hooks": [{"type": "command", "command": install_cmd}]},
            {"matcher": "", "hooks": [{"type": "command", "command": session_cmd}]},
            {"matcher": "", "hooks": [{"type": "command", "command": startup_cmd}]},
        ],
        "PostToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": error_reminder_cmd}]},
        ],
        "Stop": [
            {"hooks": [{"type": "command", "command": stop_cmd}]}
        ],
    }


def build_scribe_hooks():
    """Build Stop hook entry for the scribe transcript saver."""
    save_cmd = hook_cmd(CORE_DIR / "hooks" / "save_transcript.py", PYTHON)
    return {
        "Stop": [
            {"hooks": [{"type": "command", "command": save_cmd}]},
        ],
    }


def build_docs_hooks():
    """Build PreToolUse hook entry for the docs standards reminder."""
    remind_cmd = hook_cmd(DOCS_DIR / "hooks" / "remind_standards.py", PYTHON)
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


def write_manifest(claude_dir: Path):
    """Write .install-manifest.json with hashes of all installed files."""
    installed_files = []

    # Add all command files
    for cmd_dir in [BUDGETER_DIR / "commands", SCRIBE_DIR / "commands", CORE_DIR / "commands", DOCS_DIR / "commands", REFINER_DIR / "commands", HARDEN_DIR / "commands"]:
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

    # 5. Pre-commit hook
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

    # 6. Python
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
    all_hooks = {}
    for hooks_dict in [build_budgeter_hooks(), build_core_hooks(), build_scribe_hooks(), build_docs_hooks()]:
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

        # Pre-commit hook
        install_pre_commit_hook()

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
