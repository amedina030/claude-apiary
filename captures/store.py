"""Path resolution and storage helpers for captures.

Captures state lives at ``<git-repo-root>/.apiary/captures/``::

    tags.yaml                    — controlled tag vocabulary (per repo)
    <topic>/<slug>.md            — sidecar metadata (YAML frontmatter + body)
    <topic>/<slug>.<ext>         — image file (.png/.jpg/.jpeg/.gif/.webp/.bmp)

The image is the canonical artifact; the sidecar is metadata. Each capture
pairs exactly one image with exactly one sidecar, sharing a stem.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from researcher import _yaml_mini

APIARY_STATE_DIRNAME = ".apiary"
CAPTURES_SUBDIR = "captures"
TAGS_FILENAME = "tags.yaml"

FRONTMATTER_DELIM = "---"
SIDECAR_EXT = ".md"

ALLOWED_IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
})

ENTRY_FIELDS = (
    "title",
    "topic",
    "tags",
    "captured_at",
    "image",
    "session_id",
    "related_notes",
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


def captures_dir(start: Path | None = None) -> Path:
    """Return ``<repo-root>/.apiary/captures/``.

    Falls back to ``<cwd>/.apiary/captures/`` when not inside a git repo.
    """
    root = _git_repo_root(start) or (start or Path.cwd())
    return Path(root) / APIARY_STATE_DIRNAME / CAPTURES_SUBDIR


def tags_file(start: Path | None = None) -> Path:
    return captures_dir(start) / TAGS_FILENAME


def topic_dir(topic: str, start: Path | None = None) -> Path:
    return captures_dir(start) / topic


def sidecar_path(topic: str, slug: str, start: Path | None = None) -> Path:
    return topic_dir(topic, start) / f"{slug}{SIDECAR_EXT}"


def image_path(topic: str, slug: str, ext: str, start: Path | None = None) -> Path:
    """Return the expected image path for ``topic/slug.ext``."""
    return topic_dir(topic, start) / f"{slug}{ext}"


def find_image(topic: str, slug: str, start: Path | None = None) -> Path | None:
    """Return the image file paired with ``topic/slug``, or None if missing.

    Scans the topic directory for any file whose stem matches *slug* and
    whose extension is an allowed image extension.
    """
    d = topic_dir(topic, start)
    if not d.exists():
        return None
    for p in d.iterdir():
        if not p.is_file():
            continue
        if p.stem != slug:
            continue
        if p.suffix.lower() in ALLOWED_IMAGE_EXTENSIONS:
            return p
    return None


def ensure_layout(start: Path | None = None) -> None:
    """Create ``.apiary/captures/`` and a default ``tags.yaml`` if missing."""
    base = captures_dir(start)
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


def parse_sidecar(path: Path) -> tuple[dict[str, Any], str]:
    """Split a sidecar file into (frontmatter, body).

    Raises ``ValueError`` if the file lacks a closing frontmatter delimiter.
    """
    text = path.read_text(encoding="utf-8")
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


def write_sidecar(path: Path, frontmatter: dict[str, Any], body: str) -> None:
    """Write a sidecar file atomically. Creates parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_dump = _yaml_mini.dumps(frontmatter)
    content = f"{FRONTMATTER_DELIM}\n{fm_dump}{FRONTMATTER_DELIM}\n{body}"
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")


def list_sidecars(start: Path | None = None) -> list[Path]:
    """Return all sidecar files under captures_dir, sorted by path.

    Skips ``tags.yaml`` and any file outside a topic subdirectory.
    """
    base = captures_dir(start)
    if not base.exists():
        return []
    out: list[Path] = []
    for p in base.rglob(f"*{SIDECAR_EXT}"):
        if not p.is_file():
            continue
        if p.parent == base:
            continue
        out.append(p)
    return sorted(out)
