"""Path resolution and storage helpers for researcher.

Researcher state lives at ``<git-repo-root>/.apiary/research/``::

    tags.yaml                 — controlled tag vocabulary (per repo)
    <topic>/<slug>.md         — one research entry per file

Entries are markdown files with a YAML-subset frontmatter block followed
by standard sections (Summary / Context / Findings / Code / Caveats).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from researcher import _yaml_mini

APIARY_STATE_DIRNAME = ".apiary"
RESEARCHER_SUBDIR = "research"
TARGET_STATE_DIR_ENV = "APIARY_TARGET_STATE_DIR"
TAGS_FILENAME = "tags.yaml"

FRONTMATTER_DELIM = "---"

ENTRY_FIELDS = (
    "title",
    "topic",
    "tags",
    "date_created",
    "date_last_verified",
    "sources",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _git_repo_root(start: Path | None = None) -> Path | None:
    """Return the git repo root containing *start* (or cwd), or None."""
    cwd = str(start) if start is not None else str(Path.cwd())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top) if top else None


def research_dir(start: Path | None = None) -> Path:
    """Return the researcher state directory.

    Resolution order:
      1. ``APIARY_TARGET_STATE_DIR`` env var (set by apiary_launch.py after
         the registry resolver runs) — returns ``<state_dir>/research/``.
      2. ``<repo-root>/.apiary/research/`` via git rev-parse on *start*.
      3. ``<cwd>/.apiary/research/`` when not inside a git repo.
    """
    env_dir = os.environ.get(TARGET_STATE_DIR_ENV, "").strip()
    if env_dir:
        return Path(env_dir) / RESEARCHER_SUBDIR
    root = _git_repo_root(start) or (start or Path.cwd())
    return Path(root) / APIARY_STATE_DIRNAME / RESEARCHER_SUBDIR


def tags_file(start: Path | None = None) -> Path:
    return research_dir(start) / TAGS_FILENAME


def topic_dir(topic: str, start: Path | None = None) -> Path:
    return research_dir(start) / topic


def entry_path(topic: str, slug: str, start: Path | None = None) -> Path:
    return topic_dir(topic, start) / f"{slug}.md"


def ensure_layout(start: Path | None = None) -> None:
    """Create ``.apiary/research/`` and a default ``tags.yaml`` if missing."""
    base = research_dir(start)
    base.mkdir(parents=True, exist_ok=True)
    tags_path = tags_file(start)
    if not tags_path.exists():
        tags_path.write_text(_yaml_mini.dumps({"tags": []}), encoding="utf-8")


def normalize_topic(raw: str) -> str:
    """Normalize a topic to lowercase kebab-case ``[a-z0-9-]``."""
    collapsed = _SLUG_RE.sub("-", raw.lower()).strip("-")
    return collapsed


def slugify(title: str) -> str:
    """Derive a kebab-case slug from a title."""
    return normalize_topic(title)


def read_tags(start: Path | None = None) -> list[str]:
    """Return the list of registered tags. Empty list if tags.yaml missing."""
    path = tags_file(start)
    if not path.exists():
        return []
    data = _yaml_mini.loads(path.read_text(encoding="utf-8"))
    tags = data.get("tags", [])
    if not isinstance(tags, list):
        raise _yaml_mini.YamlParseError("'tags' must be a list", 1)
    return [str(t) for t in tags]


def write_tags(tags: list[str], start: Path | None = None) -> None:
    path = tags_file(start)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_yaml_mini.dumps({"tags": list(tags)}), encoding="utf-8")


def parse_entry(path: Path) -> tuple[dict[str, Any], str]:
    """Split an entry file into (frontmatter, body).

    Raises ``ValueError`` if the file lacks a closing frontmatter delimiter.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith(FRONTMATTER_DELIM):
        raise ValueError(f"{path} missing opening frontmatter delimiter")
    # Split on lines to find the closing ``---`` exactly.
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != FRONTMATTER_DELIM:
        raise ValueError(f"{path} missing opening frontmatter delimiter")

    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == FRONTMATTER_DELIM:
            close_idx = i
            break
    if close_idx is None:
        raise ValueError(f"{path} missing closing frontmatter delimiter")

    fm_text = "".join(lines[1:close_idx])
    body = "".join(lines[close_idx + 1:])
    frontmatter = _yaml_mini.loads(fm_text)
    return frontmatter, body


def write_entry(path: Path, frontmatter: dict[str, Any], body: str) -> None:
    """Write an entry file atomically. Creates parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_dump = _yaml_mini.dumps(frontmatter)
    content = f"{FRONTMATTER_DELIM}\n{fm_dump}{FRONTMATTER_DELIM}\n{body}"
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")


def list_entries(start: Path | None = None) -> list[Path]:
    """Return all entry files under research_dir, sorted by path."""
    base = research_dir(start)
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*.md") if p.is_file())
