"""Path resolution and storage helpers for researcher.

Researcher state lives at ``<state-dir>/research/`` where ``<state-dir>``
is the registry-allocated per-target dir (``<apiary>/.repos/<name>-<id>/``).
The launcher exports ``APIARY_TARGET_STATE_DIR`` after registry lookup.
Layout::

    tags.yaml                 — controlled tag vocabulary (per repo)
    <topic>/<slug>.md         — one research entry per file

Entries are markdown files with a YAML-subset frontmatter block followed
by standard sections (Summary / Context / Findings / Code / Caveats).
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

RESEARCHER_SUBDIR = "research"
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


def research_dir(start: Path | None = None) -> Path:
    """Return the researcher state directory.

    Delegates to :func:`core.utils.state.resolve_state_dir`, which is the
    one place the precedence lives (launcher env var → the repo's pins →
    the legacy ``.apiary/pointer`` breadcrumb → ``<repo>/.apiary/`` →
    ``<cwd>/.apiary/`` outside a git repo). See its docstring.
    """
    return resolve_state_dir(start, subdir=RESEARCHER_SUBDIR, cwd_fallback=True)


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


def parse_entry(path: Path) -> tuple[dict[str, Any], str]:
    """Split an entry file into (frontmatter, body).

    Raises ``ValueError`` if the file lacks a frontmatter block or the block
    is outside the dialect. Body bytes are preserved exactly.
    """
    text = path.read_text(encoding="utf-8")
    try:
        return frontmatter.parse(text, strict=True)
    except frontmatter.FrontmatterError as exc:
        raise ValueError(f"{path} has unreadable frontmatter: {exc}") from exc


def write_entry(path: Path, meta: dict[str, Any], body: str) -> None:
    """Write an entry file. Creates parent dirs as needed.

    Fences are always written, even for empty *meta*, so ``parse_entry``'s
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


def list_entries(start: Path | None = None) -> list[Path]:
    """Return all entry files under research_dir, sorted by path."""
    base = research_dir(start)
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*.md") if p.is_file())
