#!/usr/bin/env python3
"""
Setup script for claude-apis.

Installs all tools (budgeter hooks, clarifier agent/commands) into the Claude Code
environment. Safe to re-run — existing claude-apis entries are replaced, not duplicated.

Usage:
    # Global install (hooks fire in every Claude Code session):
    python setup.py --global

    # Per-project install (budgeter hooks only, scoped to one project):
    python setup.py --project-path /path/to/your/project
"""
import sys
import json
import shutil
import argparse
from pathlib import Path

APIS_DIR = Path(__file__).parent.resolve()
BUDGETER_DIR = APIS_DIR / "budgeter"
CLARIFIER_DIR = APIS_DIR / "clarifier"
NOTETAKER_DIR = APIS_DIR / "notetaker"

sys.path.insert(0, str(APIS_DIR))
from core.hooks import to_bash_path, hook_cmd, load_settings, save_settings, register_hooks

PYTHON = Path(sys.executable)
MARKER = "claude-apis"


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


def check_claude_md(claude_dir: Path):
    """Append clarifier rules to ~/.claude/CLAUDE.md if not already present."""
    claude_md = claude_dir / "CLAUDE.md"
    rules_src = CLARIFIER_DIR / "CLAUDE.md"
    marker = "clarifier-enabled"  # distinctive string present in the clarifier rules

    if claude_md.exists() and marker in claude_md.read_text(encoding="utf-8"):
        print(f"  CLAUDE.md        : clarifier rules detected OK")
        return

    rules = rules_src.read_text(encoding="utf-8")
    with open(claude_md, "a", encoding="utf-8") as f:
        f.write("\n" + rules)
    print(f"  CLAUDE.md        : clarifier rules appended to {claude_md}")


def install_clarifier(claude_dir: Path):
    """Copy clarifier agent, command files, and write_log.py into the Claude Code directories."""
    agents_dir = claude_dir / "agents"
    commands_dir = claude_dir / "commands"
    clarifier_bin_dir = claude_dir / "clarifier"
    agents_dir.mkdir(parents=True, exist_ok=True)
    commands_dir.mkdir(parents=True, exist_ok=True)
    clarifier_bin_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(CLARIFIER_DIR / "agents" / "clarifier.md", agents_dir / "clarifier.md")
    shutil.copy2(CLARIFIER_DIR / "commands" / "clarifier.md", commands_dir / "clarifier.md")
    shutil.copy2(CLARIFIER_DIR / "commands" / "run-clarifier-tests.md", commands_dir / "run-clarifier-tests.md")
    shutil.copy2(CLARIFIER_DIR / "write_log.py", clarifier_bin_dir / "write_log.py")
    shutil.copy2(CLARIFIER_DIR / "log_cost.py", clarifier_bin_dir / "log_cost.py")

    # Budgeter commands
    for cmd_file in (BUDGETER_DIR / "commands").glob("*.md"):
        shutil.copy2(cmd_file, commands_dir / cmd_file.name)

    # Notetaker commands
    for cmd_file in (NOTETAKER_DIR / "commands").glob("*.md"):
        shutil.copy2(cmd_file, commands_dir / cmd_file.name)

    print(f"  Clarifier agent  : {agents_dir / 'clarifier.md'}")
    print(f"  Clarifier scripts: {clarifier_bin_dir}")
    print(f"  Commands         : {commands_dir}")


def install_test_suite(claude_dir: Path):
    """Optionally install clarifier test suite files."""
    fixtures_dst = claude_dir / "test-fixtures"
    fixtures_dst.mkdir(parents=True, exist_ok=True)

    shutil.copy2(
        CLARIFIER_DIR / "test-suite" / "clarifier-test-suite.md",
        claude_dir / "clarifier-test-suite.md",
    )
    for f in (CLARIFIER_DIR / "test-suite" / "fixtures").glob("*"):
        shutil.copy2(f, fixtures_dst / f.name)

    print(f"  Test suite       : {claude_dir / 'clarifier-test-suite.md'}")
    print(f"  Test fixtures    : {fixtures_dst}")


def main():
    parser = argparse.ArgumentParser(description="Set up claude-apis tools.")
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
    parser.add_argument(
        "--with-test-suite",
        action="store_true",
        help="Also install the clarifier test suite files (global install only)",
    )
    args = parser.parse_args()

    if args.global_install:
        claude_dir = Path.home() / ".claude"
    else:
        claude_dir = Path(args.project_path).resolve() / ".claude"

    settings_path = claude_dir / "settings.json"

    # Register budgeter hooks
    new_hooks = build_budgeter_hooks()
    register_hooks(settings_path, new_hooks, MARKER, also_strip=["claude-budgeter"])

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

    # Install clarifier (global only — agent/command files live in ~/.claude)
    if args.global_install:
        install_clarifier(claude_dir)
        if args.with_test_suite:
            install_test_suite(claude_dir)
        check_claude_md(claude_dir)

    scope = "global" if args.global_install else f"project ({claude_dir.parent})"
    print(f"\n  Scope            : {scope}")
    print(f"  Hooks written to : {settings_path}")
    print(f"  Python executable: {PYTHON}")
    print(f"  claude-apis      : {APIS_DIR}")
    if not args.global_install:
        print(f"  Project config   : {claude_dir / 'budgeter.json'}")
        print(f"  Project log      : {claude_dir / 'budgeter-log.jsonl'}")
    print("\nSetup complete. Start a new Claude Code session to activate.")


if __name__ == "__main__":
    main()
