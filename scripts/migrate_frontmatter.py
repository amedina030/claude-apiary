#!/usr/bin/env python3
"""Reconcile on-disk frontmatter with the one dialect in ``core/frontmatter.py``.

Phase 3.3 replaced five hand-rolled frontmatter readers with a single dialect.
Before that, a learning's ``tags: [a, b]`` was unreadable by researcher's parser
and a research entry's block list came back as ``''`` from scribe's. This script
answers the only question that matters after the swap: **does every file already
on disk still parse to the same thing?**

Two modes:

``--check`` (default, read-only)
    Parse every file twice — once with a frozen copy of the parser that owned it
    before Phase 3.3, once with ``core.frontmatter`` — and report the files where
    the two disagree. Touches nothing.

``--apply``
    Rewrite files whose two parses agree, so the bytes on disk match what the new
    writer would emit (quoting, list style, key order). A file is rewritten only
    when all four hold: the legacy and new parses are identical, the rewrite
    round-trips through :func:`core.frontmatter.parse` back to the same
    ``(meta, body)``, the body is preserved byte-for-byte, and the file actually
    changes. Anything else is reported and skipped.

The legacy parsers below are **frozen snapshots** of the pre-3.3 code, kept only
so ``--check`` has something to compare against. Do not "fix" them — their bugs
are the point.

Usage::

    python scripts/migrate_frontmatter.py --check                 # default store
    python scripts/migrate_frontmatter.py --check --state-dir DIR
    python scripts/migrate_frontmatter.py --apply --state-dir DIR
    python scripts/migrate_frontmatter.py --check --verbose

Exit codes:
  0 — every file agrees (``--check``), or every rewrite succeeded (``--apply``)
  1 — at least one file disagrees or could not be parsed
  2 — usage error (state dir missing)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import frontmatter  # noqa: E402

EXIT_OK = 0
EXIT_DIFF = 1
EXIT_USAGE = 2

#: Directory segments never walked. ``backup``/``backups`` hold point-in-time
#: snapshots of the store — rewriting them would corrupt a restore.
SKIP_SEGMENTS = {"backup", "backups", ".git", "__pycache__"}


# --------------------------------------------------------------------------- #
# Frozen legacy parsers — pre-Phase-3.3 behaviour, for comparison only
# --------------------------------------------------------------------------- #


def _legacy_scribe(text: str) -> tuple[dict, str] | None:
    """``scribe/store.py:_parse_learning_content`` as it stood before Phase 3.3.

    Tolerant, inline ``[a, b]`` lists split on every comma (quoted or not),
    quotes stripped with ``.strip('"').strip("'")``, block lists dropped.
    """
    if not text:
        return {}, text
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}, text
    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm: dict = {}
    for raw in lines[1:end_idx]:
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "[]":
            fm[key] = []
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                fm[key] = []
            else:
                fm[key] = [
                    item.strip().strip('"').strip("'") for item in inner.split(",") if item.strip()
                ]
        else:
            fm[key] = value.strip('"').strip("'")
    body_lines = lines[end_idx + 1 :]
    body = "\n".join(body_lines)
    if text.endswith("\n") and body and not body.endswith("\n"):
        body += "\n"
    return fm, body


def _legacy_yaml_mini_loads(text: str) -> dict[str, Any]:
    """``researcher/_yaml_mini.py:loads`` as it stood after the Phase 1.6 fix.

    Block lists only — ``tags: [a, b]`` came back as the *string* ``'[a, b]'``.
    """
    result: dict[str, Any] = {}
    current_list: list[str] | None = None
    for idx, raw in enumerate(text.splitlines(), start=1):
        content = raw.strip()
        if not content or content.startswith("#"):
            continue
        leading = len(raw) - len(raw.lstrip(" "))
        if content.startswith("- ") or content == "-":
            if current_list is None:
                raise ValueError(f"line {idx}: list item without a parent key")
            current_list.append(frontmatter._parse_scalar(content[2:])[0] if content != "-" else "")
            continue
        if leading != 0:
            raise ValueError(f"line {idx}: unexpected indentation")
        if ":" not in content:
            raise ValueError(f"line {idx}: expected 'key: value'")
        key, _, value = content.partition(":")
        key = key.strip()
        if not key:
            raise ValueError(f"line {idx}: empty key")
        scalar, quoted = frontmatter._parse_scalar(value)
        if not quoted and scalar == "":
            current_list = []
            result[key] = current_list
        elif not quoted and scalar == "[]":
            current_list = None
            result[key] = []
        else:
            current_list = None
            result[key] = scalar
    return result


def _legacy_sidecar(text: str) -> tuple[dict, str] | None:
    """``researcher/store.py:parse_entry`` / ``captures/store.py:parse_sidecar``.

    Strict about the fences, then ``_yaml_mini.loads``. Returns ``None`` when
    the legacy code would have raised.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return None
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            close_idx = i
            break
    if close_idx is None:
        return None
    fm_text = "".join(lines[1:close_idx])
    body = "".join(lines[close_idx + 1 :])
    try:
        return _legacy_yaml_mini_loads(fm_text), body
    except ValueError:
        return None


def _legacy_none(text: str) -> tuple[dict, str] | None:
    """Memory files: nothing in the repo ever parsed them (knowledge.md §3).

    ``core.frontmatter`` is their first reader, so there is no prior behaviour
    to diverge from — only a question of whether they parse at all.
    """
    return None


# --------------------------------------------------------------------------- #
# Families
# --------------------------------------------------------------------------- #


class Family:
    """One kind of file: where it lives, who used to parse it, how it dumps."""

    def __init__(
        self,
        name: str,
        patterns: tuple[str, ...],
        legacy: Callable[[str], "tuple[dict, str] | None"],
        list_style: str,
    ):
        self.name = name
        self.patterns = patterns
        self.legacy = legacy
        self.list_style = list_style


FAMILIES = (
    Family("learnings", ("*/scribe/learnings/**/*.md",), _legacy_scribe, "inline"),
    Family("templates", ("*/scribe/templates/*.md",), _legacy_scribe, "inline"),
    Family("memory", ("*/scribe/memory/*.md",), _legacy_none, "block"),
    # ``researcher`` is the historical dir name; the live store uses ``research``.
    Family(
        "research",
        ("*/research/**/*.md", "*/researcher/**/*.md"),
        _legacy_sidecar,
        "block",
    ),
    Family("captures", ("*/captures/**/*.md",), _legacy_sidecar, "block"),
)


def iter_files(state_dir: Path, family: Family) -> Iterator[Path]:
    seen: set[Path] = set()
    for pattern in family.patterns:
        for path in sorted(state_dir.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            if SKIP_SEGMENTS & set(path.parts):
                continue
            seen.add(path)
            yield path


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #


class Result:
    """What happened to one file."""

    def __init__(self, path: Path, status: str, detail: str = ""):
        self.path = path
        self.status = status
        self.detail = detail


def examine(path: Path, family: Family) -> Result:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Result(path, "unreadable", str(exc))

    new_meta, new_body = frontmatter.parse(text)

    if not new_meta:
        # No frontmatter block, or one the new parser declined. Distinguish the
        # two: a block the *legacy* parser read but the new one did not is drift.
        legacy = family.legacy(text)
        if legacy is not None and legacy[0]:
            return Result(path, "differs", f"legacy read {sorted(legacy[0])}, new read nothing")
        return Result(path, "no-frontmatter")

    legacy = family.legacy(text)
    if legacy is None:
        return Result(path, "new-coverage", f"{len(new_meta)} field(s)")

    legacy_meta, _legacy_body = legacy
    if legacy_meta != new_meta:
        keys = sorted(set(legacy_meta) | set(new_meta))
        diffs = [
            f"{k}: {legacy_meta.get(k)!r} -> {new_meta.get(k)!r}"
            for k in keys
            if legacy_meta.get(k) != new_meta.get(k)
        ]
        return Result(path, "differs", "; ".join(diffs))

    rewritten = frontmatter.dump(new_meta, new_body, list_style=family.list_style)
    if rewritten == text:
        return Result(path, "canonical")
    round_tripped = frontmatter.parse(rewritten)
    if round_tripped != (new_meta, new_body):
        return Result(path, "unsafe", "rewrite does not round-trip")
    return Result(path, "rewritable", f"{len(text)} -> {len(rewritten)} bytes")


def rewrite(path: Path, family: Family) -> None:
    text = path.read_text(encoding="utf-8")
    meta, body = frontmatter.parse(text)
    path.write_text(frontmatter.dump(meta, body, list_style=family.list_style), encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

#: Statuses that mean "a human needs to look at this".
PROBLEM_STATUSES = ("differs", "unsafe", "unreadable")

STATUS_ORDER = (
    "canonical",
    "rewritable",
    "no-frontmatter",
    "new-coverage",
    "differs",
    "unsafe",
    "unreadable",
)


def default_state_dir() -> Path:
    return REPO_ROOT / ".repos"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile on-disk frontmatter with core/frontmatter.py.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Report differences without writing (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite files whose legacy and new parses agree",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=default_state_dir(),
        help="Store to walk (default: <repo>/.repos)",
    )
    parser.add_argument(
        "--family",
        action="append",
        choices=[f.name for f in FAMILIES],
        help="Limit to one family; repeatable",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="List every file, not just problems"
    )
    args = parser.parse_args(argv)

    state_dir = args.state_dir.resolve()
    if not state_dir.is_dir():
        print(f"error: state dir not found: {state_dir}", file=sys.stderr)
        return EXIT_USAGE

    selected = [f for f in FAMILIES if not args.family or f.name in args.family]
    mode_label = "apply" if args.apply else "check"
    print(f"frontmatter {mode_label}: {state_dir}")

    problems = 0
    rewritten = 0
    for family in selected:
        results = [examine(p, family) for p in iter_files(state_dir, family)]
        if not results:
            print(f"\n{family.name}: no files")
            continue
        counts = {s: 0 for s in STATUS_ORDER}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        summary = ", ".join(f"{counts[s]} {s}" for s in STATUS_ORDER if counts[s])
        print(f"\n{family.name}: {len(results)} file(s) — {summary}")

        for r in results:
            interesting = r.status in PROBLEM_STATUSES
            if interesting:
                problems += 1
            if interesting or args.verbose:
                rel = r.path.relative_to(state_dir)
                line = f"  [{r.status}] {rel}"
                if r.detail:
                    line += f" — {r.detail}"
                print(line)

        if args.apply:
            for r in results:
                if r.status != "rewritable":
                    continue
                rewrite(r.path, family)
                rewritten += 1
                print(f"  rewrote {r.path.relative_to(state_dir)}")

    print()
    if args.apply:
        print(f"rewrote {rewritten} file(s); {problems} left for review")
    else:
        print(f"{problems} file(s) need review")
    return EXIT_DIFF if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
