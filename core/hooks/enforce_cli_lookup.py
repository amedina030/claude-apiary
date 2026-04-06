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
import subprocess
import importlib.util
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CLI_LOOKUP_PATH = REPO_ROOT / 'docs' / 'reference' / 'cli_lookup.py'
CLI_TOOLS_MD = REPO_ROOT / 'docs' / 'reference' / 'cli-tools.md'
LOG_PATH = REPO_ROOT / 'core' / 'hooks' / 'enforce_cli_lookup.log'
DEFAULT_STATE_PATH = REPO_ROOT / 'core' / 'hooks' / 'served.json'


def _state_path() -> Path:
    """Per-session 'already-served' state file. Override via env for tests."""
    override = os.environ.get('ENFORCE_CLI_LOOKUP_STATE')
    return Path(override) if override else DEFAULT_STATE_PATH


def _load_served() -> dict:
    try:
        with open(_state_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _mark_served(session_id: str, tools: list[str]) -> None:
    if not session_id or not tools:
        return
    try:
        state = _load_served()
        existing = set(state.get(session_id, []))
        existing.update(tools)
        state[session_id] = sorted(existing)
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f)
    except OSError:
        pass


def _already_served(session_id: str, tool: str) -> bool:
    if not session_id:
        return False
    return tool in _load_served().get(session_id, [])


def _run_cli_lookup(basename: str) -> str:
    """Run cli_lookup.py for `basename` and return its stdout (or a fallback)."""
    try:
        result = subprocess.run(
            [sys.executable, str(CLI_LOOKUP_PATH), basename],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (result.stdout or result.stderr or '(cli_lookup produced no output)').rstrip()
    except Exception as e:
        return f'(cli_lookup.py invocation failed: {e})'


def _emit_deny(reason: str) -> None:
    """Print a PreToolUse deny response (modern hookSpecificOutput schema) and exit 0."""
    out = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason,
        }
    }
    print(json.dumps(out))
    sys.exit(0)


def _log(decision: str, reason: str, session_id: str = '', command: str = '') -> None:
    """Append a one-line forensic record. Never raises."""
    try:
        import datetime
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cmd_snip = (command or '').replace('\n', ' ')[:200]
        rec = {
            'ts': ts,
            'decision': decision,
            'reason': reason,
            'session_id': session_id,
            'command': cmd_snip,
        }
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass


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
    session_id = ''
    command = ''
    try:
        raw = sys.stdin.read()
        if not raw:
            _log('allow', 'empty_stdin')
            _fail_open()

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            _log('allow', 'bad_json')
            _fail_open()

        if payload.get('tool_name') != 'Bash':
            _log('allow', 'not_bash', payload.get('session_id') or '')
            sys.exit(0)

        tool_input = payload.get('tool_input') or {}
        command = tool_input.get('command') or ''
        session_id = payload.get('session_id') or ''
        if not command:
            _log('allow', 'empty_command', session_id, command)
            sys.exit(0)

        cwd = payload.get('cwd') or os.getcwd()

        known = _load_known_tools()
        if not known:
            _log('allow', 'known_tools_empty', session_id, command)
            sys.exit(0)

        subcommands = _split_subcommands(command)
        if not subcommands:
            _log('allow', 'no_subcommands', session_id, command)
            sys.exit(0)

        missing: list[str] = []
        looked_up: list[str] = []
        unmatched_count = 0
        for subcmd in subcommands:
            if _is_cli_lookup_invocation(subcmd):
                continue
            tokens = _tokens(subcmd)
            matched = _match_tool(tokens, known)
            if matched is None:
                unmatched_count += 1
                continue
            project_key = _project_key_from_cwd(cwd)
            transcript_path = (
                Path.home() / '.claude' / 'projects' / project_key / f'{session_id}.jsonl'
            )
            if _transcript_has_lookup_for(transcript_path, matched):
                looked_up.append(matched)
            elif _already_served(session_id, matched):
                # Hook served docs for this tool earlier in the session via deny
                # path. Treat as looked-up so the retry can proceed.
                looked_up.append(matched)
            else:
                missing.append(matched)

        if not missing:
            if looked_up:
                reason = f'lookup_found:{",".join(sorted(set(looked_up)))}'
            else:
                reason = f'no_known_tool_in_command(unmatched={unmatched_count})'
            _log('allow', reason, session_id, command)
            sys.exit(0)

        # Deduplicate preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for m in missing:
            if m not in seen:
                seen.add(m)
                deduped.append(m)

        # Build a single deny message that includes the cli_lookup output for
        # every missing tool, so Claude has the docs in context on retry.
        sections: list[str] = [
            'Blocked by enforce_cli_lookup: the following repo CLI tool(s) were'
            ' invoked without a prior `cli_lookup.py` call in this session:'
            f' {", ".join(deduped)}.',
            'The hook has fetched the lookup output below. Read it, then retry'
            ' your original command — the hook will allow it on the next attempt.',
        ]
        for m in deduped:
            basename = m.rsplit('/', 1)[-1]
            docs = _run_cli_lookup(basename)
            sections.append(f'--- cli_lookup.py {basename} ---\n{docs}')

        _mark_served(session_id, deduped)
        _log('block', f'missing:{",".join(deduped)}', session_id, command)
        _emit_deny('\n\n'.join(sections))

    except SystemExit:
        raise
    except Exception as e:
        _log('allow', f'internal_error:{e}', session_id, command)
        _fail_open(f'enforce_cli_lookup internal error: {e}')


if __name__ == '__main__':
    main()
