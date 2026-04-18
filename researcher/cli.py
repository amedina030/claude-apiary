#!/usr/bin/env python3
"""Researcher CLI — manage structured research findings per repo.

State lives at ``<repo>/.apiary/research/`` with a ``tags.yaml`` controlled
vocabulary and ``<topic>/<slug>.md`` entries.

Usage:
    cli.py add <topic> <title> [--tags t1,t2,...]
    cli.py find <query>
    cli.py list [--topic X] [--tag Y]
    cli.py show <topic> <slug>
    cli.py verify <topic> <slug>
    cli.py register-tag <tag>

Exit codes:
    0  success
    2  validation error (unknown tag, duplicate slug, not found, etc.)
    3  config/YAML parse error (invalid tags.yaml or frontmatter)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from researcher import _yaml_mini, store

TEMPLATE_PATH = Path(__file__).resolve().parent / "template.md"
DEFAULT_FIND_LIMIT = 10
SUMMARY_PREVIEW_LEN = 200

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_CONFIG = 3


def _load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _parse_tags_arg(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _today_iso() -> str:
    return date.today().isoformat()


def _safe_read_tags(start: Path | None = None) -> list[str] | int:
    """Return registered tags or an exit code on YAML failure."""
    try:
        return store.read_tags(start)
    except _yaml_mini.YamlParseError as exc:
        print(
            f"error: {store.tags_file(start)}: {exc.message} (line {exc.line})",
            file=sys.stderr,
        )
        return EXIT_CONFIG


def _extract_summary(body: str) -> str:
    """Return the first non-empty block under ``## Summary``."""
    lines = body.splitlines()
    in_summary = False
    collected: list[str] = []
    for line in lines:
        if line.strip().lower().startswith("## summary"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## "):
                break
            collected.append(line)
    text = "\n".join(collected).strip()
    if len(text) > SUMMARY_PREVIEW_LEN:
        text = text[:SUMMARY_PREVIEW_LEN].rstrip() + "..."
    return text


def cmd_add(args: argparse.Namespace) -> int:
    topic = store.normalize_topic(args.topic)
    if not topic:
        print("error: topic cannot be empty after normalization", file=sys.stderr)
        return EXIT_VALIDATION
    slug = store.slugify(args.title)
    if not slug:
        print("error: title cannot be empty after slugification", file=sys.stderr)
        return EXIT_VALIDATION

    store.ensure_layout()

    tags_in = _parse_tags_arg(args.tags)
    registered = _safe_read_tags()
    if isinstance(registered, int):
        return registered

    unknown = [t for t in tags_in if t not in registered]
    if unknown:
        print(
            f"error: unknown tag(s): {', '.join(unknown)}",
            file=sys.stderr,
        )
        if registered:
            print(f"known tags: {', '.join(registered)}", file=sys.stderr)
        else:
            print(
                "no tags registered yet; use 'register-tag <tag>' first",
                file=sys.stderr,
            )
        return EXIT_VALIDATION

    path = store.entry_path(topic, slug)
    if path.exists():
        print(
            f"error: entry already exists at {path}; "
            f"use 'verify {topic} {slug}' to bump the last-verified date",
            file=sys.stderr,
        )
        return EXIT_VALIDATION

    today = _today_iso()
    frontmatter = {
        "title": args.title,
        "topic": topic,
        "tags": list(tags_in),
        "date_created": today,
        "date_last_verified": today,
        "sources": [],
    }
    body = _load_template()
    store.write_entry(path, frontmatter, body)

    print(str(path))
    print(f"tags: {', '.join(tags_in) if tags_in else '(none)'}")
    print(
        "warning: entry was scaffolded from the template; "
        "fill in Summary / Context / Findings before relying on it",
        file=sys.stderr,
    )
    return EXIT_OK


def _rank_hits(query: str, entries: list[Path]) -> list[tuple[int, Path, dict, str]]:
    q = query.lower()
    hits: list[tuple[int, Path, dict, str]] = []
    for path in entries:
        try:
            fm, body = store.parse_entry(path)
        except (ValueError, _yaml_mini.YamlParseError):
            continue
        score = 0
        title = str(fm.get("title", "")).lower()
        tags = [str(t).lower() for t in fm.get("tags", [])]
        content = body.lower()
        if q in title:
            score += 3
        score += 2 * sum(1 for t in tags if q in t)
        score += content.count(q)
        if score > 0:
            hits.append((score, path, fm, body))
    hits.sort(key=lambda h: (-h[0], str(h[1])))
    return hits


def cmd_find(args: argparse.Namespace) -> int:
    query = args.query.strip()
    if not query:
        print("error: query cannot be empty", file=sys.stderr)
        return EXIT_VALIDATION

    entries = store.list_entries()
    ranked = _rank_hits(query, entries)
    if not ranked:
        print(
            f"no matches for {query!r}; "
            "consider WebSearch and capture findings with 'add'",
            file=sys.stderr,
        )
        return EXIT_OK

    limit = args.limit or DEFAULT_FIND_LIMIT
    for score, path, fm, body in ranked[:limit]:
        title = fm.get("title", "(untitled)")
        tags = fm.get("tags", []) or []
        summary = _extract_summary(body)
        print(f"=== {path} ===")
        print(f"title: {title}")
        print(f"tags:  {', '.join(tags) if tags else '(none)'}")
        print(f"score: {score}")
        if summary:
            print(f"summary: {summary}")
        print()
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    entries = store.list_entries()
    if not entries:
        print("(no research entries)", file=sys.stderr)
        return EXIT_OK

    registered = _safe_read_tags()
    if isinstance(registered, int):
        return registered

    if args.tag and args.tag not in registered:
        print(
            f"warning: tag {args.tag!r} is not in controlled vocabulary",
            file=sys.stderr,
        )

    grouped: dict[str, list[tuple[Path, dict]]] = {}
    for path in entries:
        try:
            fm, _body = store.parse_entry(path)
        except (ValueError, _yaml_mini.YamlParseError):
            continue
        topic = str(fm.get("topic", "(unknown)"))
        if args.topic and topic != args.topic:
            continue
        tags = [str(t) for t in fm.get("tags", []) or []]
        if args.tag and args.tag not in tags:
            continue
        grouped.setdefault(topic, []).append((path, fm))

    if not grouped:
        print("(no matching entries)", file=sys.stderr)
        return EXIT_OK

    for topic in sorted(grouped):
        print(f"## {topic}")
        for path, fm in grouped[topic]:
            title = fm.get("title", "(untitled)")
            tags = fm.get("tags", []) or []
            tag_str = ", ".join(tags) if tags else "(none)"
            print(f"  {path.stem}  —  {title}  [{tag_str}]")
        print()
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    topic = store.normalize_topic(args.topic)
    slug = store.slugify(args.slug)
    path = store.entry_path(topic, slug)
    if not path.exists():
        print(f"error: not found: {path}", file=sys.stderr)
        return EXIT_VALIDATION
    print(path.read_text(encoding="utf-8"), end="")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    topic = store.normalize_topic(args.topic)
    slug = store.slugify(args.slug)
    path = store.entry_path(topic, slug)
    if not path.exists():
        print(f"error: not found: {path}", file=sys.stderr)
        return EXIT_VALIDATION
    try:
        fm, body = store.parse_entry(path)
    except (ValueError, _yaml_mini.YamlParseError) as exc:
        print(f"error: failed to parse {path}: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    old = fm.get("date_last_verified", "(none)")
    new = _today_iso()
    fm["date_last_verified"] = new
    store.write_entry(path, fm, body)
    print(f"{old} -> {new}")
    return EXIT_OK


def cmd_register_tag(args: argparse.Namespace) -> int:
    tag = args.tag.strip()
    if not tag:
        print("error: tag cannot be empty", file=sys.stderr)
        return EXIT_VALIDATION
    store.ensure_layout()
    registered = _safe_read_tags()
    if isinstance(registered, int):
        return registered
    if tag in registered:
        print(f"error: tag {tag!r} already registered", file=sys.stderr)
        return EXIT_VALIDATION
    registered.append(tag)
    store.write_tags(registered)
    print("tags:")
    for t in registered:
        print(f"  - {t}")
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="researcher",
        description="Manage structured research findings (apiary researcher subsystem).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_add = subparsers.add_parser("add", help="Add a new research entry")
    p_add.add_argument("topic")
    p_add.add_argument("title")
    p_add.add_argument("--tags", help="Comma-separated tags")

    p_find = subparsers.add_parser("find", help="Search entries")
    p_find.add_argument("query")
    p_find.add_argument("--limit", type=int, default=DEFAULT_FIND_LIMIT)

    p_list = subparsers.add_parser("list", help="List entries grouped by topic")
    p_list.add_argument("--topic")
    p_list.add_argument("--tag")

    p_show = subparsers.add_parser("show", help="Print entry contents")
    p_show.add_argument("topic")
    p_show.add_argument("slug")

    p_verify = subparsers.add_parser("verify", help="Bump date_last_verified to today")
    p_verify.add_argument("topic")
    p_verify.add_argument("slug")

    p_reg = subparsers.add_parser("register-tag", help="Add a tag to the vocabulary")
    p_reg.add_argument("tag")

    return parser


COMMANDS = {
    "add": cmd_add,
    "find": cmd_find,
    "list": cmd_list,
    "show": cmd_show,
    "verify": cmd_verify,
    "register-tag": cmd_register_tag,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
