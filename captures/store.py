"""Path resolution and storage helpers for captures.

Captures state lives at ``<state-dir>/captures/`` where ``<state-dir>``
is the registry-allocated per-target dir (``<apiary>/.repos/<name>-<id>/``).
The launcher exports ``APIARY_TARGET_STATE_DIR`` after registry lookup.
Layout::

    tags.yaml                    — controlled tag vocabulary (per repo)
    <topic>/<slug>.md            — sidecar metadata (YAML frontmatter + body)
    <topic>/<slug>.<ext>         — image file (.png/.jpg/.jpeg/.gif/.webp/.bmp)

The image is the canonical artifact; the sidecar is metadata. Each capture
pairs exactly one image with exactly one sidecar, sharing a stem.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import frontmatter  # noqa: E402

# Path resolution and the two names that describe it live in core.utils.state -
# re-exported here so callers (and tests) can keep saying ``store.<NAME>``
# while there is exactly one definition in the tree (review X-3).
from core.utils.state import (  # noqa: F401
    LEGACY_STATE_DIRNAME as APIARY_STATE_DIRNAME,
)
from core.utils.state import (
    resolve_state_dir,
)

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


def captures_dir(start: Path | None = None) -> Path:
    """Return the captures state directory.

    Delegates to :func:`core.utils.state.resolve_state_dir`, which is the
    one place the precedence lives (launcher env var → the repo's pins →
    the legacy ``.apiary/pointer`` breadcrumb → ``<repo>/.apiary/`` →
    ``<cwd>/.apiary/`` outside a git repo). See its docstring.
    """
    return resolve_state_dir(start, subdir=CAPTURES_SUBDIR, cwd_fallback=True)


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
        tags_path.write_text(frontmatter.dumps({"tags": []}), encoding="utf-8")


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
    data = frontmatter.loads(path.read_text(encoding="utf-8"))
    tags = data.get("tags", [])
    if not isinstance(tags, list):
        raise frontmatter.FrontmatterError("'tags' must be a list", 1)
    return [str(t) for t in tags]


def write_tags(tags: list[str], start: Path | None = None) -> None:
    path = tags_file(start)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps({"tags": list(tags)}), encoding="utf-8")


def parse_sidecar(path: Path) -> tuple[dict[str, Any], str]:
    """Split a sidecar file into (frontmatter, body).

    Raises ``ValueError`` if the file lacks a frontmatter block or the block
    is outside the dialect. Body bytes are preserved exactly.
    """
    text = path.read_text(encoding="utf-8")
    try:
        return frontmatter.parse(text, strict=True)
    except frontmatter.FrontmatterError as exc:
        raise ValueError(f"{path} has unreadable frontmatter: {exc}") from exc


def write_sidecar(path: Path, meta: dict[str, Any], body: str) -> None:
    """Write a sidecar file. Creates parent dirs as needed.

    Fences are always written, even for empty *meta*, so ``parse_sidecar``'s
    strict read of the file it just wrote always succeeds.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if meta:
        content = frontmatter.dump(meta, body)
    else:
        content = f"{FRONTMATTER_DELIM}\n{FRONTMATTER_DELIM}\n{body}"
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
