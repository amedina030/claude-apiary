#!/usr/bin/env python3
"""Captures CLI — manage visual captures (screenshots, mockups, etc.) per repo.

State lives at ``<repo>/.apiary/captures/`` with a ``tags.yaml`` controlled
vocabulary and ``<topic>/<slug>.<ext>`` image files paired with
``<topic>/<slug>.md`` sidecar metadata.

Usage:
    cli.py add <topic> <image-path> --title "<t>" [--tags t1,t2,...]
           [--context "<text>"] [--related ID1,ID2] [--session-id <id>]
           [--move]
    cli.py find <query>
    cli.py list [--topic X] [--tag Y]
    cli.py show <topic> <slug>
    cli.py path <topic> <slug>
    cli.py register-tag <tag>

Exit codes:
    0  success
    2  validation error (unknown tag, duplicate slug, not found, bad ext)
    3  config/YAML parse error (invalid tags.yaml or frontmatter)
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from captures import store
from core import frontmatter

DEFAULT_FIND_LIMIT = 10
CONTEXT_PREVIEW_LEN = 200

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_CONFIG = 3


def _parse_csv_arg(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _today_iso() -> str:
    return date.today().isoformat()


def _safe_read_tags(start: Path | None = None) -> list[str] | int:
    """Return registered tags or an exit code on YAML failure."""
    try:
        return store.read_tags(start)
    except frontmatter.FrontmatterError as exc:
        print(
            f"error: {store.tags_file(start)}: {exc.message} (line {exc.line})",
            file=sys.stderr,
        )
        return EXIT_CONFIG


def _clip_preview(text: str, limit: int = CONTEXT_PREVIEW_LEN) -> str:
    stripped = text.strip()
    if len(stripped) > limit:
        return stripped[:limit].rstrip() + "..."
    return stripped


def cmd_add(args: argparse.Namespace) -> int:
    topic = store.normalize_topic(args.topic)
    if not topic:
        print("error: topic cannot be empty after normalization", file=sys.stderr)
        return EXIT_VALIDATION
    slug = store.slugify(args.title)
    if not slug:
        print("error: title cannot be empty after slugification", file=sys.stderr)
        return EXIT_VALIDATION

    src = Path(args.image_path).expanduser().resolve()
    if not src.exists() or not src.is_file():
        print(f"error: image not found: {src}", file=sys.stderr)
        return EXIT_VALIDATION
    ext = src.suffix.lower()
    if ext not in store.ALLOWED_IMAGE_EXTENSIONS:
        allowed = ", ".join(sorted(store.ALLOWED_IMAGE_EXTENSIONS))
        print(
            f"error: unsupported image extension {ext!r}; allowed: {allowed}",
            file=sys.stderr,
        )
        return EXIT_VALIDATION

    store.ensure_layout()

    tags_in = _parse_csv_arg(args.tags)
    registered = _safe_read_tags()
    if isinstance(registered, int):
        return registered

    unknown = [t for t in tags_in if t not in registered]
    if unknown:
        print(f"error: unknown tag(s): {', '.join(unknown)}", file=sys.stderr)
        if registered:
            print(f"known tags: {', '.join(registered)}", file=sys.stderr)
        else:
            print(
                "no tags registered yet; use 'register-tag <tag>' first",
                file=sys.stderr,
            )
        return EXIT_VALIDATION

    sidecar = store.sidecar_path(topic, slug)
    if sidecar.exists() or store.find_image(topic, slug) is not None:
        print(
            f"error: capture already exists at topic={topic} slug={slug}; "
            "pick a different title or delete the existing entry first",
            file=sys.stderr,
        )
        return EXIT_VALIDATION

    dest_image = store.image_path(topic, slug, ext)
    dest_image.parent.mkdir(parents=True, exist_ok=True)
    if args.move:
        shutil.move(str(src), str(dest_image))
    else:
        shutil.copy2(str(src), str(dest_image))

    today = _today_iso()
    frontmatter: dict = {
        "title": args.title,
        "topic": topic,
        "tags": list(tags_in),
        "captured_at": today,
        "image": dest_image.name,
    }
    if args.session_id:
        frontmatter["session_id"] = args.session_id
    frontmatter["related_notes"] = _parse_csv_arg(args.related)
    frontmatter["sources"] = []
    body = (args.context or "").strip()
    if body and not body.endswith("\n"):
        body += "\n"
    store.write_sidecar(sidecar, frontmatter, body)

    print(str(dest_image))
    print(str(sidecar))
    print(f"tags: {', '.join(tags_in) if tags_in else '(none)'}")
    return EXIT_OK


def _rank_hits(query: str, sidecars: list[Path]) -> list[tuple[int, Path, dict, str]]:
    q = query.lower()
    hits: list[tuple[int, Path, dict, str]] = []
    for path in sidecars:
        try:
            fm, body = store.parse_sidecar(path)
        except ValueError:
            continue
        score = 0
        title = str(fm.get("title", "")).lower()
        tags = [str(t).lower() for t in fm.get("tags", []) or []]
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

    sidecars = store.list_sidecars()
    ranked = _rank_hits(query, sidecars)
    if not ranked:
        print(
            f"no captures match {query!r}; "
            "add one with 'captures add <topic> <image-path>'",
            file=sys.stderr,
        )
        return EXIT_OK

    limit = args.limit or DEFAULT_FIND_LIMIT
    for score, path, fm, body in ranked[:limit]:
        title = fm.get("title", "(untitled)")
        tags = fm.get("tags", []) or []
        topic = fm.get("topic", "(unknown)")
        slug = path.stem
        image = store.find_image(str(topic), slug)
        print(f"=== {path} ===")
        print(f"title:  {title}")
        print(f"topic:  {topic}")
        print(f"slug:   {slug}")
        print(f"image:  {image if image else '(missing)'}")
        print(f"tags:   {', '.join(tags) if tags else '(none)'}")
        print(f"score:  {score}")
        preview = _clip_preview(body)
        if preview:
            print(f"context: {preview}")
        print()
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    sidecars = store.list_sidecars()
    if not sidecars:
        print("(no captures)", file=sys.stderr)
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
    for path in sidecars:
        try:
            fm, _body = store.parse_sidecar(path)
        except ValueError:
            continue
        topic = str(fm.get("topic", "(unknown)"))
        if args.topic and topic != args.topic:
            continue
        tags = [str(t) for t in fm.get("tags", []) or []]
        if args.tag and args.tag not in tags:
            continue
        grouped.setdefault(topic, []).append((path, fm))

    if not grouped:
        print("(no matching captures)", file=sys.stderr)
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
    sidecar = store.sidecar_path(topic, slug)
    if not sidecar.exists():
        print(f"error: not found: {sidecar}", file=sys.stderr)
        return EXIT_VALIDATION
    print(sidecar.read_text(encoding="utf-8"), end="")
    image = store.find_image(topic, slug)
    if image:
        print(f"\nimage: {image.resolve()}")
    else:
        print("\nimage: (missing)", file=sys.stderr)
    return EXIT_OK


def cmd_path(args: argparse.Namespace) -> int:
    topic = store.normalize_topic(args.topic)
    slug = store.slugify(args.slug)
    image = store.find_image(topic, slug)
    if image is None:
        print(
            f"error: no image found for topic={topic} slug={slug}",
            file=sys.stderr,
        )
        return EXIT_VALIDATION
    print(str(image.resolve()))
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
        prog="captures",
        description="Manage visual captures (apiary captures subsystem).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_add = subparsers.add_parser("add", help="Add a new capture from an image path")
    p_add.add_argument("topic")
    p_add.add_argument("image_path", help="Path to a source image file")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--tags", help="Comma-separated tags")
    p_add.add_argument("--context", help="Free-text description (body)")
    p_add.add_argument(
        "--related",
        help="Comma-separated related scribe note IDs (e.g. T-2026-209,C-2026-42)",
    )
    p_add.add_argument("--session-id", help="Session ID to stamp on the capture")
    p_add.add_argument(
        "--move",
        action="store_true",
        help="Move the source file into the store instead of copying",
    )

    p_find = subparsers.add_parser("find", help="Search captures by title, tags, context")
    p_find.add_argument("query")
    p_find.add_argument("--limit", type=int, default=DEFAULT_FIND_LIMIT)

    p_list = subparsers.add_parser("list", help="List captures grouped by topic")
    p_list.add_argument("--topic")
    p_list.add_argument("--tag")

    p_show = subparsers.add_parser("show", help="Print sidecar + image path")
    p_show.add_argument("topic")
    p_show.add_argument("slug")

    p_path = subparsers.add_parser("path", help="Print the absolute image path only")
    p_path.add_argument("topic")
    p_path.add_argument("slug")

    p_reg = subparsers.add_parser("register-tag", help="Add a tag to the vocabulary")
    p_reg.add_argument("tag")

    return parser


COMMANDS = {
    "add": cmd_add,
    "find": cmd_find,
    "list": cmd_list,
    "show": cmd_show,
    "path": cmd_path,
    "register-tag": cmd_register_tag,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
