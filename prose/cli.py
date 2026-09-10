#!/usr/bin/env python3
"""Prose linter CLI: check files, calibrate the rules against a corpus, list rules.

Usage (via the per-repo launcher)::

    prose/cli.py check <path>... [--format text|json] [--min-level L]
    prose/cli.py check --stdin [--format text|json]
    prose/cli.py calibrate <dir>... [--min-level L] [--top N]
    prose/cli.py rules [--verbose]

Exit codes:
    check      1 when any finding is at ``error`` level, else 0. Warnings and
               suggestions print but do not fail, so the same command serves
               a CI step and an advisory pass.
    calibrate  0 always. It is a measurement, not a gate.
    rules      0.

``check`` on a directory walks its ``*.md`` files. ``calibrate`` is how a rule
change is judged: run it over the notes and docs a reader has already
accepted, and an ``error``-level rule that fires there is miscalibrated.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prose import engine

EXIT_OK = 0
EXIT_ERRORS = 1


def _repo() -> "Path | None":
    try:
        from core.flags import _per_repo_root

        return _per_repo_root()
    except Exception:  # noqa: BLE001 — outside a repo the shipped config is fine
        return None


def _markdown_files(roots: list[Path], config: dict) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        if not root.is_dir():
            print(f"{root}: not a file or directory", file=sys.stderr)
            continue
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            if engine.path_included(rel, config):
                files.append(path)
    return files


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def cmd_check(args) -> int:
    config = engine.load_config(_repo())
    rules = engine.load_rules(config)
    min_level = args.min_level or config["min_level"]
    all_findings: list[engine.Finding] = []
    outputs: list[str] = []

    if args.stdin:
        text = sys.stdin.read()
        findings = engine.filter_level(
            engine.check_text(text, path="<stdin>", config=config, rules=rules), min_level
        )
        all_findings.extend(findings)
        outputs.append(engine.format_text(findings, "<stdin>"))
    else:
        for path in _markdown_files([Path(p) for p in args.paths], config):
            findings = engine.filter_level(
                engine.check_path(path, config=config, rules=rules), min_level
            )
            all_findings.extend(findings)
            outputs.append(engine.format_text(findings, str(path)))

    if args.format == "json":
        print(engine.format_json(all_findings))
    else:
        print("\n\n".join(outputs))
        if engine.has_level(all_findings, "error"):
            print(
                f"\n{engine.counts(all_findings)['error']} error-level finding(s). See {engine.PROSE_DIR.name}/styles and docs/standards/prose-style.md."
            )
    return EXIT_ERRORS if engine.has_level(all_findings, "error") else EXIT_OK


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------


def calibrate(
    roots: list[Path], config: dict, rules: list[engine.Rule], min_level: str, top: int
) -> list[str]:
    """Per-rule and per-file counts over *roots*; returns the report lines."""
    lines: list[str] = []
    grand_files = grand_clean = 0
    grand_rule_files: Counter = Counter()
    grand_rule_hits: Counter = Counter()
    level_of = {r.id: r.level for r in rules}

    for root in roots:
        files = _markdown_files([root], config)
        if not files:
            lines.append(f"== {root}: no markdown files")
            continue
        clean = 0
        words = 0
        rule_files: Counter = Counter()
        rule_hits: Counter = Counter()
        per_file: dict[Path, list[engine.Finding]] = {}
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                lines.append(f"   ! {path}: {exc}")
                continue
            words += sum(engine.md.word_count(b.prose) for b in engine.md.blocks(text) if b.prose)
            findings = engine.filter_level(
                engine.check_text(text, path=str(path), config=config, rules=rules), min_level
            )
            if not findings:
                clean += 1
                continue
            per_file[path] = findings
            for rule_id in {f.rule for f in findings}:
                rule_files[rule_id] += 1
            for f in findings:
                rule_hits[f.rule] += 1
        n = len(files)
        lines.append(
            f"== {root}: {n} files, {clean} clean ({100 * clean // n}%), {n - clean} flagged, ~{words} words"
        )
        for rule_id, nf in rule_files.most_common():
            lines.append(
                f"   {rule_id:22s} files {nf:4d}  hits {rule_hits[rule_id]:4d}  [{level_of.get(rule_id, '?')}]"
            )
        for path, findings in sorted(per_file.items(), key=lambda kv: -len(kv[1]))[:top]:
            lines.append(f"   worst: {path.name} ({len(findings)} findings)")
            for f in findings[:4]:
                lines.append(f"      - line {f.line} [{f.level}] {f.rule}: {f.context[:110]}")
        grand_files += n
        grand_clean += clean
        grand_rule_files.update(rule_files)
        grand_rule_hits.update(rule_hits)

    if len(roots) > 1 and grand_files:
        lines.append(
            f"== totals: {grand_files} files, {grand_clean} clean ({100 * grand_clean // grand_files}%), "
            f"{grand_files - grand_clean} flagged"
        )
        for rule_id, nf in grand_rule_files.most_common():
            lines.append(
                f"   {rule_id:22s} files {nf:4d}  hits {grand_rule_hits[rule_id]:4d}  [{level_of.get(rule_id, '?')}]"
            )
    return lines


def cmd_calibrate(args) -> int:
    config = engine.load_config(_repo())
    rules = engine.load_rules(config)
    min_level = args.min_level or "suggestion"
    for line in calibrate([Path(p) for p in args.dirs], config, rules, min_level, args.top):
        print(line)
    return EXIT_OK


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def cmd_rules(args) -> int:
    config = engine.load_config(_repo())
    rules = engine.load_rules(config)
    for r in rules:
        print(f"{r.id:22s} {r.level:10s} {','.join(r.scopes):18s} {r.summary}")
        if args.verbose:
            for line in r.why.strip().splitlines():
                print(f"    {line}")
            if r.credit:
                print(f"    credit: {r.credit}")
            print()
    if not rules:
        print("no rules loaded (check 'styles' / 'style_paths' in prose/config.json)")
    return EXIT_OK


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prose/cli.py", description="Markup-aware prose linter for machine-writing tells"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser(
        "check", help="Check markdown files (or --stdin); exit 1 on error-level findings"
    )
    p_check.add_argument(
        "paths", nargs="*", help="Files or directories (directories are walked for *.md)"
    )
    p_check.add_argument("--stdin", action="store_true", help="Read the text to check from stdin")
    p_check.add_argument("--format", choices=("text", "json"), default="text", help="Output format")
    p_check.add_argument(
        "--min-level",
        choices=engine.LEVELS,
        default=None,
        help="Lowest level to report (default: config min_level)",
    )
    p_check.set_defaults(func=cmd_check)

    p_cal = sub.add_parser("calibrate", help="Per-rule hit counts over a corpus of accepted prose")
    p_cal.add_argument("dirs", nargs="+", help="Directories to walk for *.md")
    p_cal.add_argument(
        "--min-level",
        choices=engine.LEVELS,
        default=None,
        help="Lowest level to count (default: suggestion)",
    )
    p_cal.add_argument("--top", type=int, default=3, help="Worst files to list per directory")
    p_cal.set_defaults(func=cmd_calibrate)

    p_rules = sub.add_parser("rules", help="List the loaded rules")
    p_rules.add_argument(
        "--verbose", action="store_true", help="Also print each rule's why and credit"
    )
    p_rules.set_defaults(func=cmd_rules)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check" and not args.stdin and not args.paths:
        parser.error("check needs at least one path, or --stdin")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
