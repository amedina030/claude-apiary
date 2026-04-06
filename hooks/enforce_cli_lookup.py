#!/usr/bin/env python3
"""PreToolUse Bash hook that blocks repo CLI tool invocations unless the current
session's transcript shows a prior cli_lookup.py call for that tool.

Reads a Claude Code PreToolUse JSON payload from stdin. If the Bash command
invokes a known repo CLI tool without a preceding cli_lookup.py call in the
session transcript, exits with code 2 and a descriptive message so Claude Code
feeds the block reason back to the model.

Fails open (exit 0) on any internal error so a buggy hook never wedges the session.
"""

import sys
import json
import os
import re
import shlex
import importlib.util
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_LOOKUP_PATH = REPO_ROOT / 'docs' / 'reference' / 'cli_lookup.py'
CLI_TOOLS_MD = REPO_ROOT / 'docs' / 'reference' / 'cli-tools.md'


def _fail_open(msg: str = '') -> None:
    if msg:
        print(msg, file=sys.stderr)
    sys.exit(0)


def _load_known_tools() -> list[str]:
    try:
        spec = importlib.util.spec_from_file_location('cli_lookup_helper', CLI_LOOKUP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.list_known_tools()
    except Exception:
        return []


def _project_key_from_cwd(cwd: str) -> str:
    return ''.join('-' if c in (':', '/', '\\') else c for c in cwd)


def _split_subcommands(cmd: str) -> list[str]:
    pieces = re.split(r'\s*(?:&&|\|\||;|\|)\s*', cmd)
    return [p.strip() for p in pieces if p.strip()]


def _tokens(subcmd: str) -> list[str]:
    try:
        return shlex.split(subcmd, posix=True)
    except ValueError:
        return subcmd.split()


def _match_tool(tokens: list[str], known: list[str]) -> Optional[str]:
    for token in tokens:
        token = token.strip('"\'')
        for known_path in known:
            basename = known_path.rsplit('/', 1)[-1]
            if (
                token == known_path
                or token.endswith('/' + known_path)
                or token.endswith('\\' + known_path)
                or token == basename
                or token.endswith('/' + basename)
                or token.endswith('\\' + basename)
            ):
                return known_path
    return None


def _is_cli_lookup_invocation(subcmd: str) -> bool:
    return any('cli_lookup.py' in t for t in _tokens(subcmd))


def _bash_command_looks_up(command: str, tool: str, basename: str) -> bool:
    """True if `command` is an actual cli_lookup.py invocation referencing tool."""
    for sub in _split_subcommands(command):
        if not _is_cli_lookup_invocation(sub):
            continue
        toks = _tokens(sub)
        # Argument to cli_lookup.py — match tool path or basename (with or
        # without .py) as a token suffix.
        bare = basename[:-3] if basename.endswith('.py') else basename
        for t in toks:
            t = t.strip('"\'')
            if (
                t == tool
                or t.endswith('/' + tool)
                or t.endswith('\\' + tool)
                or t == basename
                or t == bare
            ):
                return True
    return False


def _iter_bash_commands(obj):
    """Yield every Bash tool_use command string found anywhere in obj."""
    if isinstance(obj, dict):
        if obj.get('type') == 'tool_use' and obj.get('name') == 'Bash':
            cmd = (obj.get('input') or {}).get('command')
            if isinstance(cmd, str):
                yield cmd
        for v in obj.values():
            yield from _iter_bash_commands(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_bash_commands(v)


def _transcript_has_lookup_for(transcript_path: Path, tool: str) -> bool:
    if not transcript_path.exists():
        return False
    basename = tool.rsplit('/', 1)[-1]
    try:
        with open(transcript_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or 'cli_lookup.py' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for cmd in _iter_bash_commands(obj):
                    if 'cli_lookup.py' not in cmd:
                        continue
                    if _bash_command_looks_up(cmd, tool, basename):
                        return True
    except OSError:
        return False
    return False


def main():
    try:
        raw = sys.stdin.read()
        if not raw:
            _fail_open()

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            _fail_open()

        if payload.get('tool_name') != 'Bash':
            sys.exit(0)

        tool_input = payload.get('tool_input') or {}
        command = tool_input.get('command') or ''
        if not command:
            sys.exit(0)

        session_id = payload.get('session_id') or ''
        cwd = payload.get('cwd') or os.getcwd()

        known = _load_known_tools()
        if not known:
            sys.exit(0)

        subcommands = _split_subcommands(command)
        if not subcommands:
            sys.exit(0)

        missing: list[str] = []
        for subcmd in subcommands:
            if _is_cli_lookup_invocation(subcmd):
                continue
            tokens = _tokens(subcmd)
            matched = _match_tool(tokens, known)
            if matched is None:
                continue
            project_key = _project_key_from_cwd(cwd)
            transcript_path = (
                Path.home() / '.claude' / 'projects' / project_key / f'{session_id}.jsonl'
            )
            if not _transcript_has_lookup_for(transcript_path, matched):
                missing.append(matched)

        if not missing:
            sys.exit(0)

        # Deduplicate preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for m in missing:
            if m not in seen:
                seen.add(m)
                deduped.append(m)

        for m in deduped:
            basename = m.rsplit('/', 1)[-1]
            print(
                f'Blocked: run `python docs/reference/cli_lookup.py {basename}` before using'
                f' {m} in this session (cli_lookup.py teaches the correct flags).',
                file=sys.stderr,
            )

        sys.exit(2)

    except Exception as e:
        _fail_open(f'enforce_cli_lookup internal error: {e}')


if __name__ == '__main__':
    main()
