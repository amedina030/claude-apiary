#!/usr/bin/env python3
"""Portability audit script — reports shell-mode subprocess, literal null devices,
and text-mode open() calls without explicit encoding."""

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOW = {
    'runner/validate_plan.py',
    'runner/test_validate_plan.py',
    'runner/auto_plan.py',
    'core/hooks/enforce_cli_lookup.py',
    'scripts/audit_portability.py',
}

SHELL_RE = re.compile(r'\bshell\s*=\s*True\b')
NULL_RE = re.compile(r'(?:\bNUL\b|\bnul\b|/dev/null)')

Violation = tuple[str, str, int, str]


def discover_files() -> list[str]:
    result = subprocess.run(
        ['git', 'ls-files', '*.py'],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        normalized = line.replace('\\', '/')
        if normalized in ALLOW:
            continue
        paths.append(normalized)
    return paths


def check_shell_mode(path_str: str, lines: list[str]) -> list[Violation]:
    violations = []
    for lineno, line in enumerate(lines, 1):
        if 'noqa: shell-true' in line:
            continue
        if SHELL_RE.search(line):
            violations.append(('shell-mode', path_str, lineno, line.strip()))
    return violations


def check_null_device(path_str: str, lines: list[str]) -> list[Violation]:
    violations = []
    for lineno, line in enumerate(lines, 1):
        if 'noqa: null-device' in line:
            continue
        if NULL_RE.search(line):
            violations.append(('null-device', path_str, lineno, line.strip()))
    return violations


def _has_keyword(keywords: list[ast.keyword], name: str) -> bool:
    return any(kw.arg == name for kw in keywords)


def check_open_encoding(path_str: str, source: str) -> tuple[list[Violation], bool]:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [('syntax', path_str, 0, str(e))], True

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        if isinstance(func, ast.Name) and func.id == 'open':
            mode = 'r'
            if len(node.args) >= 2:
                arg1 = node.args[1]
                if isinstance(arg1, ast.Constant) and isinstance(arg1.value, str):
                    mode = arg1.value
            for kw in node.keywords:
                if kw.arg == 'mode' and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    mode = kw.value.value
            if 'b' in mode:
                continue
            if not _has_keyword(node.keywords, 'encoding'):
                violations.append(('open-no-encoding', path_str, node.lineno, ast.unparse(node)[:100]))

        elif isinstance(func, ast.Attribute) and func.attr in ('read_text', 'write_text'):
            if not _has_keyword(node.keywords, 'encoding'):
                violations.append(('text-no-encoding', path_str, node.lineno, ast.unparse(node)[:100]))

    return violations, False


def main() -> None:
    parser = argparse.ArgumentParser(description='Portability audit for claude-apiary Python files.')
    parser.add_argument('--fix-list', action='store_true', help='Write violations as JSON to stdout')
    parser.add_argument('--quiet', action='store_true', help='Suppress per-file output')
    args = parser.parse_args()

    rel_paths = discover_files()
    all_violations: list[Violation] = []
    has_errors = False

    for rel in rel_paths:
        abs_path = REPO_ROOT / rel
        try:
            source = abs_path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            all_violations.append(('encoding', rel, 0, 'file is not valid UTF-8'))
            has_errors = True
            continue
        except FileNotFoundError:
            continue

        lines = source.splitlines()

        file_violations: list[Violation] = []
        file_violations.extend(check_shell_mode(rel, lines))
        file_violations.extend(check_null_device(rel, lines))

        enc_violations, had_syntax = check_open_encoding(rel, source)
        file_violations.extend(enc_violations)
        if had_syntax:
            has_errors = True

        if file_violations and not args.quiet and not args.fix_list:
            for v in file_violations:
                kind, path, lineno, detail = v
                loc = f'{path}:{lineno}' if lineno else path
                print(f'  [{kind}] {loc}')
                if detail:
                    print(f'    {detail}')

        all_violations.extend(file_violations)

    if args.fix_list:
        json.dump(
            [{'type': v[0], 'path': v[1], 'lineno': v[2], 'detail': v[3]} for v in all_violations],
            sys.stdout,
            indent=2,
        )
        print()
        sys.exit(0 if (not all_violations and not has_errors) else 1)

    by_type: dict[str, list[Violation]] = {}
    for v in all_violations:
        by_type.setdefault(v[0], []).append(v)

    if not args.quiet:
        print('\n--- Portability Audit Summary ---')
        if not all_violations:
            print('No violations found.')
        else:
            for kind, items in sorted(by_type.items()):
                print(f'  {kind}: {len(items)}')
            print(f'  TOTAL: {len(all_violations)}')

    sys.exit(0 if (not all_violations and not has_errors) else 1)


if __name__ == '__main__':
    main()
