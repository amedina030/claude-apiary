"""Per-type note templates, and the required-section gate on ``add``.

A template is a markdown file at ``<scribe-state-dir>/templates/<type>.md``.
Most are guidance the model can read with ``notes.py template show <type>``
and never block anything. A template whose frontmatter declares ``required:``
sections makes ``add`` reject content that omits one of them — one check, one
attempt, ``--force`` to bypass (2026-08 review §5a-B, option C; the
hash-acknowledgement flow that preceded it is gone).

Forward-only by design: the gate runs on ``add``, never against notes already
written, so editing a template only changes what comes next.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe.store import VALID_TYPES

#: Subdirectory of the scribe state dir holding the live templates.
TEMPLATES_DIRNAME = "templates"

#: Templates shipped with apiary (tracked in git). ``apiary install`` copies
#: these into a target's live templates dir — see :func:`scaffold_defaults`.
#: Not the live template dir.
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parent / "default_templates"

#: Records the hash of each bundled template as scaffolded, so a refresh can
#: tell "the user never touched this" from "the user edited it".
_HASH_RECORD = ".bundled_hashes.json"


def template_path(state_dir: Path, note_type: str) -> Path:
    """The template file for a note type. May or may not exist."""
    return Path(state_dir) / TEMPLATES_DIRNAME / f"{note_type}.md"


def template_text(state_dir: Path, note_type: str) -> "str | None":
    """The template body for a note type, or None if missing/empty.

    A whitespace-only file counts as missing — both skip the gate. A read
    error also returns None, so a transient I/O failure cannot lock someone
    out of writing notes.
    """
    path = template_path(state_dir, note_type)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text if text.strip() else None


def required_sections(text: str) -> list:
    """The ``required:`` section names a template's frontmatter declares.

    Empty when there is no ``required:`` key or it is not a list. Parsed in
    tolerant mode, so a malformed template never raises here — it just stops
    enforcing.
    """
    from core import frontmatter  # one dialect for the whole toolkit

    meta, _ = frontmatter.parse(text)
    req = meta.get("required")
    if not isinstance(req, list):
        return []
    return [str(s).strip() for s in req if str(s).strip()]


def section_present(content: str, name: str) -> bool:
    """True when *content* carries section *name*.

    Either as a Markdown heading at any level (``### What was done``) or as a
    bold inline label (``**Why:**``), case-insensitive — the two shapes
    handoffs and decisions actually use.
    """
    esc = re.escape(name)
    if re.search(rf"(?im)^\s*#{{1,6}}\s+{esc}\b", content):
        return True
    return bool(re.search(rf"(?i)\*\*\s*{esc}\s*:?\s*\*\*", content))


def missing_sections(content: str, required: list) -> list:
    """The subset of *required* sections absent from *content*."""
    return [s for s in required if not section_present(content, s)]


def describe(state_dir: Path, note_type: str) -> str:
    """One-line label for a type's template: what it enforces, if anything."""
    text = template_text(state_dir, note_type)
    if text is None:
        return "no template"
    req = required_sections(text)
    return f"required: {', '.join(req)}" if req else "guidance only"


def overview(state_dir: Path) -> list:
    """One line per type that has a template: name, what it enforces, path."""
    return [
        f"{t:10s} {describe(state_dir, t):60s} {template_path(state_dir, t)}"
        for t in VALID_TYPES
        if template_text(state_dir, t) is not None
    ]


def render(state_dir: Path, note_type: str) -> "str | None":
    """The ``template show`` body for a type, or None when there is none."""
    text = template_text(state_dir, note_type)
    if text is None:
        return None
    return (
        f"# template for {note_type} ({describe(state_dir, note_type)})\n"
        f"{text}{'' if text.endswith(chr(10)) else chr(10)}"
    )


def check(state_dir: Path, note_type: str, content: str) -> tuple:
    """Run the gate. Returns ``(missing_sections, template_text)``.

    ``missing`` is empty whenever the note passes — no template, a
    guidance-only template, or every required section present. The template
    text comes back so the caller can inline it in a rejection message,
    which is the difference between a retry that works and one that guesses.
    """
    text = template_text(state_dir, note_type)
    if text is None:
        return ([], None)
    required = required_sections(text)
    if not required:
        return ([], text)
    return (missing_sections(content, required), text)


def gate(state_dir: Path, note_type: str, content: str, *, force: bool = False) -> tuple:
    """Run the gate for an ``add``. Returns ``(message, fatal)``.

    ``message`` is what to print on stderr — None when the note passes, the
    ``--force`` bypass line when it was waved through, and the rejection
    (template inlined, so the retry has what it needs) otherwise. ``fatal``
    says whether the add must stop.
    """
    missing, text = check(state_dir, note_type, content)
    if not missing:
        return (None, False)
    if force:
        return (
            f"[template gate bypassed via --force: {note_type} note missing {', '.join(missing)}]",
            False,
        )
    return (
        f"content missing required section(s): {', '.join(missing)}\n"
        f"  template: {template_path(state_dir, note_type)}\n\n"
        f"--- {note_type}.md ---\n"
        f"{text.rstrip()}\n"
        f"--- end ---\n\n"
        f"Add the missing section(s) and re-run, or pass --force to bypass.",
        True,
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scaffold_defaults(state_dir: Path) -> list:
    """Copy the bundled per-type templates into ``<state-dir>/templates/``.

    **Never overwrites an edited file.** A template whose current hash still
    matches what we recorded when we wrote it is one nobody has touched, so a
    newer bundled version replaces it; anything else is the user's and is left
    alone. That is what makes this safe to call on every ``apiary install``.
    Returns the sorted note types actually written.
    """
    dst_dir = Path(state_dir) / TEMPLATES_DIRNAME
    record_path = dst_dir / _HASH_RECORD
    try:
        recorded = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(recorded, dict):
            recorded = {}
    except (OSError, ValueError):
        recorded = {}

    written: list = []
    for note_type in VALID_TYPES:
        src = DEFAULT_TEMPLATES_DIR / f"{note_type}.md"
        if not src.is_file():
            continue
        dst = dst_dir / f"{note_type}.md"
        bundled = src.read_text(encoding="utf-8")
        if dst.exists():
            current = dst.read_text(encoding="utf-8")
            untouched = recorded.get(note_type) == _sha(current)
            if not untouched or current == bundled:
                continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst.write_text(bundled, encoding="utf-8")
        recorded[note_type] = _sha(bundled)
        written.append(note_type)

    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(recorded, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
    return sorted(written)
