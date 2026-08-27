"""Shared library for the context-rules system (#224).

Loads rule files from `context-rules/<category>/<id>.md`, renders them
into the managed zone format that `core/install.py` injects into a
bootstrapped repo's `<repo>/CLAUDE.md`, and parses an existing managed
zone back into structured form for drift detection.

The renderer is path-agnostic — it takes and returns text; the caller
owns the target file.

Rule file format:

    ---
    id: <id>
    title: <title>
    category: <category>
    requires: []
    ---
    <body markdown>

Managed zone format inside `<repo>/CLAUDE.md`:

    <!-- apiary-context-rules-start -->
    <!-- apiary-context-rule:<id> hash=<sha256> -->
    <body>
    <!-- /apiary-context-rule:<id> -->
    ...
    <!-- apiary-context-rules-end -->

Stdlib only — no PyYAML. Frontmatter is intentionally restricted to a flat
key/value form so the parser stays small and deterministic.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_DIR = REPO_ROOT / "context-rules"

OUTER_START = "<!-- apiary-context-rules-start -->"
OUTER_END = "<!-- apiary-context-rules-end -->"

# Inner sentinel format. Hash is the sha256 of the rule body as written
# under the start sentinel (no trailing newline normalization beyond
# whatever load_rule produced — render and parse must agree).
_INNER_START_RE = re.compile(
    r"<!--\s*apiary-context-rule:(?P<id>[a-zA-Z0-9_\-]+)\s+hash=(?P<hash>[0-9a-f]{64})\s*-->"
)
_INNER_END_TEMPLATE = "<!-- /apiary-context-rule:{id} -->"
_INNER_END_RE = re.compile(
    r"<!--\s*/apiary-context-rule:(?P<id>[a-zA-Z0-9_\-]+)\s*-->"
)

REQUIRED_FRONTMATTER_FIELDS = ("id", "title", "category", "requires")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """A single context rule loaded from disk."""

    id: str
    title: str
    category: str
    requires: tuple[str, ...]
    body: str
    source_path: Path

    @property
    def body_hash(self) -> str:
        return hash_body(self.body)


@dataclass
class InstalledRule:
    """A rule block parsed out of an existing managed zone."""

    id: str
    stored_hash: str
    body: str

    @property
    def body_hash(self) -> str:
        return hash_body(self.body)


@dataclass
class ManagedZone:
    """Parsed view of the managed zone in a repo's CLAUDE.md."""

    start: int  # char offset of OUTER_START
    end: int  # char offset just after OUTER_END
    rules: list[InstalledRule] = field(default_factory=list)
    # Any non-rule content found inside the zone — should be empty for a
    # well-formed zone. Non-empty means tampering.
    stray_text: str = ""


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def hash_body(body: str) -> str:
    """Return sha256 hex digest of a rule body."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class RuleParseError(ValueError):
    """Raised when a rule file cannot be parsed."""


def _parse_frontmatter(text: str, source: Path) -> tuple[dict, str]:
    """Split a rule file into (frontmatter_dict, body). Body has trailing
    newline normalized to exactly one.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise RuleParseError(f"{source}: missing opening '---' frontmatter fence")

    # Locate closing fence.
    lines = text.splitlines(keepends=False)
    if lines[0] != "---":
        raise RuleParseError(f"{source}: malformed opening fence")
    end_idx: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end_idx = i
            break
    if end_idx is None:
        raise RuleParseError(f"{source}: missing closing '---' frontmatter fence")

    fm: dict = {}
    for raw in lines[1:end_idx]:
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise RuleParseError(f"{source}: bad frontmatter line: {raw!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        fm[key] = _coerce_value(value)

    body_lines = lines[end_idx + 1 :]
    body = "\n".join(body_lines).strip("\n") + "\n"
    return fm, body


def _coerce_value(value: str):
    """Coerce a frontmatter value string into list/str."""
    if value == "[]":
        return []
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip('"').strip("'") for item in inner.split(",")]
    return value.strip('"').strip("'")


def load_rule(path: Path) -> Rule:
    """Load and validate a single rule file."""
    text = path.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text, path)

    missing = [f for f in REQUIRED_FRONTMATTER_FIELDS if f not in fm]
    if missing:
        raise RuleParseError(
            f"{path}: missing required frontmatter field(s): {', '.join(missing)}"
        )

    if fm["id"] != path.stem:
        raise RuleParseError(
            f"{path}: id '{fm['id']}' does not match filename stem '{path.stem}'"
        )

    parent = path.parent.name
    if fm["category"] != parent:
        raise RuleParseError(
            f"{path}: category '{fm['category']}' does not match parent dir '{parent}'"
        )

    requires = fm["requires"]
    if not isinstance(requires, list):
        raise RuleParseError(f"{path}: requires must be a list, got {type(requires).__name__}")

    if not body.strip():
        raise RuleParseError(f"{path}: body is empty")

    return Rule(
        id=fm["id"],
        title=fm["title"],
        category=fm["category"],
        requires=tuple(requires),
        body=body,
        source_path=path,
    )


def load_all_rules(rules_dir: Path = RULES_DIR) -> list[Rule]:
    """Load every rule under rules_dir, sorted by (category, id)."""
    rules: list[Rule] = []
    if not rules_dir.is_dir():
        return rules
    for path in sorted(rules_dir.glob("*/*.md")):
        rules.append(load_rule(path))
    rules.sort(key=lambda r: (r.category, r.id))
    return rules


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_rule_block(rule: Rule) -> str:
    """Render a single rule into its inner sentinel block."""
    body = rule.body if rule.body.endswith("\n") else rule.body + "\n"
    return (
        f"<!-- apiary-context-rule:{rule.id} hash={rule.body_hash} -->\n"
        f"{body}"
        f"{_INNER_END_TEMPLATE.format(id=rule.id)}\n"
    )


def render_managed_zone(rules: list[Rule]) -> str:
    """Render the full managed zone from a list of rules."""
    parts = [OUTER_START, ""]
    for rule in rules:
        parts.append(render_rule_block(rule).rstrip("\n"))
        parts.append("")
    parts.append(OUTER_END)
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Parsing an existing CLAUDE.md
# ---------------------------------------------------------------------------


class ZoneTamperError(ValueError):
    """Raised when the managed zone is malformed in a way that requires --force."""


def find_managed_zone(text: str) -> Optional[ManagedZone]:
    """Locate and parse the managed zone in `text`. Returns None if no zone
    is present (clean install case). Raises ZoneTamperError on malformed
    structure (duplicate markers, missing end, mismatched inner blocks).
    """
    starts = [m.start() for m in re.finditer(re.escape(OUTER_START), text)]
    ends = [m.start() for m in re.finditer(re.escape(OUTER_END), text)]

    if not starts and not ends:
        return None

    if len(starts) != 1 or len(ends) != 1:
        raise ZoneTamperError(
            f"managed zone is malformed: found {len(starts)} start marker(s)"
            f" and {len(ends)} end marker(s); expected exactly one of each"
        )

    start_pos = starts[0]
    end_marker_pos = ends[0]
    if end_marker_pos < start_pos:
        raise ZoneTamperError("managed zone end marker appears before start marker")

    end_pos = end_marker_pos + len(OUTER_END)
    inner = text[start_pos + len(OUTER_START) : end_marker_pos]

    rules: list[InstalledRule] = []
    stray_chunks: list[str] = []

    cursor = 0
    for m in _INNER_START_RE.finditer(inner):
        between = inner[cursor : m.start()]
        if between.strip():
            stray_chunks.append(between)
        rid = m.group("id")
        stored_hash = m.group("hash")
        body_start = m.end()
        # Find matching end
        end_match = _INNER_END_RE.search(inner, body_start)
        if end_match is None:
            raise ZoneTamperError(
                f"rule '{rid}' is missing its closing sentinel"
            )
        if end_match.group("id") != rid:
            raise ZoneTamperError(
                f"rule '{rid}' end sentinel id mismatch: got '{end_match.group('id')}'"
            )
        body = inner[body_start : end_match.start()]
        body = body.lstrip("\n")
        if not body.endswith("\n"):
            body += "\n"
        rules.append(InstalledRule(id=rid, stored_hash=stored_hash, body=body))
        cursor = end_match.end()

    trailing = inner[cursor:]
    if trailing.strip():
        stray_chunks.append(trailing)

    return ManagedZone(
        start=start_pos,
        end=end_pos,
        rules=rules,
        stray_text="".join(stray_chunks),
    )


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


@dataclass
class DriftReport:
    """Result of comparing the installed managed zone to the source rules."""

    missing_source: list[str] = field(default_factory=list)  # installed but no source file
    not_installed: list[str] = field(default_factory=list)  # source exists, not installed
    hash_mismatch: list[str] = field(default_factory=list)  # installed but body changed
    tampered_bodies: list[str] = field(default_factory=list)  # body hash != stored hash
    stray_text: bool = False
    zone_present: bool = False

    @property
    def total(self) -> int:
        return (
            len(self.missing_source)
            + len(self.not_installed)
            + len(self.hash_mismatch)
            + len(self.tampered_bodies)
            + (1 if self.stray_text else 0)
        )

    @property
    def clean(self) -> bool:
        return self.total == 0


def compute_drift(
    claude_md_text: str, source_rules: list[Rule] | None = None
) -> DriftReport:
    """Compare claude_md_text's managed zone against source_rules.

    `not_installed` is reported relative to the *currently installed* set —
    a rule that exists on disk but was never installed is NOT drift, it is
    a user choice. We only report `not_installed` for rules that the user
    has clearly opted into via the source list passed in. For Layer 2 use,
    callers pass `source_rules=None` to suppress not_installed reporting.
    """
    report = DriftReport()
    try:
        zone = find_managed_zone(claude_md_text)
    except ZoneTamperError:
        report.stray_text = True
        return report

    if zone is None:
        return report

    report.zone_present = True
    if zone.stray_text:
        report.stray_text = True

    by_id = {r.id: r for r in (source_rules or load_all_rules())}
    installed_ids = {ir.id for ir in zone.rules}

    for ir in zone.rules:
        if ir.id not in by_id:
            report.missing_source.append(ir.id)
            continue
        # body tampering: stored hash mismatch
        if ir.body_hash != ir.stored_hash:
            report.tampered_bodies.append(ir.id)
            continue
        # source drift: stored hash != current source hash
        if ir.stored_hash != by_id[ir.id].body_hash:
            report.hash_mismatch.append(ir.id)

    if source_rules is not None:
        for r in source_rules:
            if r.id not in installed_ids:
                report.not_installed.append(r.id)

    return report
