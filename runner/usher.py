#!/usr/bin/env python3
"""
Usher — ticket sizing gate for the autonomous runner.

Reads an intake JSON, extracts the scope field, counts distinct files and
top-level directories (subsystems), measures description length, and compares
against configurable thresholds.  Produces a pass/warn/fail verdict.

Exit 0 on pass or warn, exit 1 on fail (overridden by --warn-only).

Usage:
    python -m runner.usher <path_to_intake.json> [--warn-only]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import PurePosixPath

from .config_loader import get as cfg

# Matches repo-relative paths like "runner/run.py", "core/utils/filelock.py"
_FILE_PATH_RE = re.compile(r"(?:[\w.-]+/)+[\w.-]+\.[\w]+")

_DEFAULT_THRESHOLDS = {
    "max_files": {"pass": 5, "warn": 8},
    "max_subsystems": {"pass": 2, "warn": 3},
    "max_description_chars": {"pass": 2000, "warn": 4000},
}


def _extract_file_paths(scope: str) -> list[str]:
    """Extract deduplicated file paths from a scope string."""
    if not isinstance(scope, str) or not scope:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for match in _FILE_PATH_RE.finditer(scope):
        path = match.group()
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _extract_subsystems(file_paths: list[str]) -> set[str]:
    """Return the set of top-level directories from a list of file paths."""
    subsystems: set[str] = set()
    for p in file_paths:
        parts = PurePosixPath(p).parts
        if len(parts) > 1:
            subsystems.add(parts[0])
    return subsystems


def score(data: dict) -> dict:
    """Score an intake dict on sizing metrics. Pure function, no config access."""
    scope = data.get("scope", "")
    description = data.get("description", "")
    file_paths = _extract_file_paths(scope)
    subsystems = _extract_subsystems(file_paths)
    return {
        "file_count": len(file_paths),
        "subsystem_count": len(subsystems),
        "description_chars": len(description.strip()),
        "files": file_paths,
        "subsystems": sorted(subsystems),
    }


def assess(data: dict, config: dict | None = None) -> tuple[str, dict]:
    """Assess an intake dict against sizing thresholds.

    Returns (verdict, metrics) where verdict is 'pass', 'warn', or 'fail'.
    If *config* is None, thresholds are loaded from runner/config.json.
    """
    if config is None:
        config = {}
        for key, default in _DEFAULT_THRESHOLDS.items():
            config[key] = cfg("usher", key, default)

    metrics = score(data)

    checks = [
        ("file_count", "max_files"),
        ("subsystem_count", "max_subsystems"),
        ("description_chars", "max_description_chars"),
    ]

    worst = "pass"
    per_metric: dict[str, str] = {}

    for metric_key, config_key in checks:
        value = metrics[metric_key]
        thresholds = config.get(config_key, _DEFAULT_THRESHOLDS[config_key])
        pass_limit = thresholds["pass"]
        warn_limit = thresholds["warn"]

        if value > warn_limit:
            per_metric[metric_key] = "fail"
        elif value > pass_limit:
            per_metric[metric_key] = "warn"
        else:
            per_metric[metric_key] = "pass"

        # Escalate worst verdict
        if per_metric[metric_key] == "fail":
            worst = "fail"
        elif per_metric[metric_key] == "warn" and worst != "fail":
            worst = "warn"

    metrics["per_metric"] = per_metric
    return worst, metrics


def _format_verdict(verdict: str, metrics: dict) -> str:
    """Format a human-readable verdict string."""
    lines = [f"Usher: {verdict}"]
    per = metrics.get("per_metric", {})
    lines.append(f"  files:       {metrics['file_count']:>3}  ({per.get('file_count', '?')})")
    lines.append(
        f"  subsystems:  {metrics['subsystem_count']:>3}  ({per.get('subsystem_count', '?')})"
    )
    lines.append(
        f"  desc chars:  {metrics['description_chars']:>3}  ({per.get('description_chars', '?')})"
    )
    if metrics.get("files"):
        lines.append(f"  file list:   {', '.join(metrics['files'])}")
    if metrics.get("subsystems"):
        lines.append(f"  subsystems:  {', '.join(metrics['subsystems'])}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Usher — ticket sizing gate")
    parser.add_argument("file", help="Path to intake JSON file")
    parser.add_argument(
        "--warn-only",
        dest="warn_only",
        action="store_true",
        default=False,
        help="Print verdict but always exit 0",
    )
    args = parser.parse_args()

    try:
        with open(args.file, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print("Expected a JSON object", file=sys.stderr)
        sys.exit(1)

    if "scope" not in data:
        print("Usher: fail — cannot assess: no scope field", file=sys.stderr)
        sys.exit(1)

    verdict, metrics = assess(data)
    print(_format_verdict(verdict, metrics))

    if verdict == "fail" and not args.warn_only:
        sys.exit(1)


if __name__ == "__main__":
    main()
