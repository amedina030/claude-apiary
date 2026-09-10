"""Rule engine for the prose linter.

Rules are data: one TOML file per rule under ``prose/styles/<Style>/``, with
a check type (``extends``), a message, a level, the scopes it applies to and
a ``why`` recording where its threshold came from. The design follows Vale's
(markup-aware scopes, rules as files, three severity levels, spans in the
output) without its binary: apiary is stdlib-only at runtime, so the format
is TOML (``tomllib``) and the Markdown scoping is ``prose/markdown.py``.

Check types:

============  =============================================================
existence     report every match of ``tokens`` (literal, word-bounded) or
              ``raw`` (Python regex) in the scoped text
substitution  like existence, with a ``swap`` table of pattern -> preferred
occurrence    count ``token`` per scoped block; report a block over ``max``
              or under ``min``
density       count ``token`` over all scoped blocks of the file against a
              per-``per_words`` budget: ``allowed = max(floor, int(words *
              rate / per_words))``. Vale has no density check; the fixed
              maximum it offers flags a short note and passes a long one
repetition    a word repeated back to back (``the the``)
============  =============================================================

``check_text`` is the whole API for callers: text in, a sorted list of
:class:`Finding` out. ``format_text``/``format_json`` render them, and
``summary_lines`` renders the rule set itself as the short reminder the hook
injects, so that reminder can never drift from the rules.
"""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from prose import markdown as md

LEVELS = ("suggestion", "warning", "error")
LEVEL_RANK = {name: i for i, name in enumerate(LEVELS)}
CHECK_TYPES = ("existence", "substitution", "occurrence", "density", "repetition")

PROSE_DIR = Path(__file__).resolve().parent
STYLES_DIR = PROSE_DIR / "styles"
CONFIG_PATH = PROSE_DIR / "config.json"
#: Per-repo override, the checklist's ``.claude/<tool>.json`` convention.
PROJECT_CONFIG_REL = Path(".claude") / "prose.json"

DEFAULT_CONFIG: dict = {
    "styles": ["Tells"],
    "style_paths": [],
    "min_level": "suggestion",
    "disabled_rules": [],
    "include_globs": ["**/*.md"],
    "exclude_globs": [".repos/**", "_tmp_*/**", "node_modules/**", ".venv/**", ".git/**"],
    "context_chars": 40,
}

#: Characters of matched text kept in a finding's span before truncation.
SPAN_MAX = 96


class RuleError(ValueError):
    """A rule file that cannot be loaded. The message names the file."""


@dataclass(frozen=True)
class Rule:
    id: str
    style: str
    extends: str
    message: str
    summary: str
    level: str
    scopes: tuple[str, ...]
    why: str = ""
    credit: str = ""
    enabled: bool = True
    patterns: tuple = ()  # existence / repetition: compiled regexes
    swaps: tuple = ()  # substitution: (compiled regex, replacement)
    # occurrence / density. Unquoted on purpose: a quoted annotation on a field
    # named ``token`` reads as a credential to scripts/secret_scan.py.
    token: re.Pattern | None = None
    min: "int | None" = None
    max: "int | None" = None
    per_words: int = 100
    rate: float = 1.0
    floor: int = 1

    @property
    def rank(self) -> int:
        return LEVEL_RANK[self.level]


@dataclass
class Finding:
    rule: str
    level: str
    line: int
    span: str
    context: str
    message: str
    scope: str
    path: "str | None" = None

    @property
    def rank(self) -> int:
        return LEVEL_RANK[self.level]

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(repo: "Path | None" = None) -> dict:
    """Shipped defaults, then ``prose/config.json``, then ``<repo>/.claude/prose.json``.

    Shallow merge: a list in the override replaces the list below it, so a
    repo can narrow ``include_globs`` or disable rules without inheriting.
    """
    config = dict(DEFAULT_CONFIG)
    config.update(_read_json(CONFIG_PATH))
    if repo is not None:
        config.update(_read_json(Path(repo) / PROJECT_CONFIG_REL))
    if config.get("min_level") not in LEVELS:
        config["min_level"] = DEFAULT_CONFIG["min_level"]
    return config


def path_included(rel_path: str, config: dict) -> bool:
    """Whether the hook should check *rel_path* (posix-style, repo-relative)."""
    rel = re.sub(r"^(?:\./)+", "", rel_path.replace("\\", "/"))
    name = rel.rsplit("/", 1)[-1]
    if any(
        fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(name, g) for g in config.get("exclude_globs", [])
    ):
        return False
    return any(
        fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(name, g.rsplit("/", 1)[-1])
        for g in config.get("include_globs", [])
    )


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------


def _require(data: dict, key: str, path: Path, kind=str):
    value = data.get(key)
    if (
        value is None
        or (kind is str and not isinstance(value, str))
        or (kind is list and not isinstance(value, list))
    ):
        raise RuleError(f"{path}: missing or invalid {key!r}")
    return value


def _flags(data: dict) -> int:
    flags = re.MULTILINE
    if data.get("ignorecase", False):
        flags |= re.IGNORECASE
    return flags


def _compile(pattern: str, flags: int, path: Path) -> "re.Pattern":
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise RuleError(f"{path}: bad regex {pattern!r}: {exc}") from exc


def _bounded(literal: str, nonword: bool) -> str:
    escaped = re.escape(literal)
    if nonword:
        return escaped
    # \b only means something next to a word character; a token that starts
    # or ends with punctuation gets no boundary on that side.
    left = r"\b" if literal[:1].isalnum() else ""
    right = r"\b" if literal[-1:].isalnum() else ""
    return f"{left}{escaped}{right}"


def _default_summary(message: str) -> str:
    text = re.sub(r"\s*'%s'\s*", " ", message).replace("%s", "").strip()
    return re.sub(r"\s{2,}", " ", text)


def parse_rule(path: Path, style: str) -> Rule:
    """Load one rule file. Raises :class:`RuleError` naming the file on any defect."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuleError(f"{path}: {exc}") from exc

    extends = _require(data, "extends", path)
    if extends not in CHECK_TYPES:
        raise RuleError(
            f"{path}: unknown extends {extends!r}; expected one of {', '.join(CHECK_TYPES)}"
        )
    message = _require(data, "message", path)
    level = data.get("level", "warning")
    if level not in LEVELS:
        raise RuleError(f"{path}: unknown level {level!r}; expected one of {', '.join(LEVELS)}")
    scope = data.get("scope", ["text"])
    scopes = tuple([scope] if isinstance(scope, str) else scope)
    for s in scopes:
        if s not in md.SCOPES:
            raise RuleError(f"{path}: unknown scope {s!r}; expected one of {', '.join(md.SCOPES)}")
    flags = _flags(data)
    nonword = bool(data.get("nonword", False))
    fields = dict(
        id=path.stem,
        style=style,
        extends=extends,
        message=message,
        summary=data.get("summary") or _default_summary(message),
        level=level,
        scopes=scopes,
        why=data.get("why", ""),
        credit=data.get("credit", ""),
        enabled=bool(data.get("enabled", True)),
    )

    if extends in ("existence",):
        tokens = data.get("tokens", [])
        raw = data.get("raw", [])
        if not tokens and not raw:
            raise RuleError(f"{path}: existence rule needs 'tokens' or 'raw'")
        patterns = [_compile(_bounded(t, nonword), flags, path) for t in tokens]
        patterns += [_compile(r, flags, path) for r in raw]
        fields["patterns"] = tuple(patterns)
    elif extends == "substitution":
        swap = data.get("swap")
        if not isinstance(swap, dict) or not swap:
            raise RuleError(f"{path}: substitution rule needs a non-empty 'swap' table")
        fields["swaps"] = tuple(
            (_compile(_bounded(k, nonword) if k.replace(" ", "").isalnum() else k, flags, path), v)
            for k, v in swap.items()
        )
    elif extends in ("occurrence", "density"):
        token = _require(data, "token", path)
        fields["token"] = _compile(token, flags, path)
        if extends == "occurrence":
            fields["min"] = data.get("min")
            fields["max"] = data.get("max")
            if fields["min"] is None and fields["max"] is None:
                raise RuleError(f"{path}: occurrence rule needs 'min' or 'max'")
        else:
            fields["per_words"] = int(data.get("per_words", 100))
            fields["rate"] = float(data.get("rate", 1.0))
            fields["floor"] = int(data.get("floor", 1))
    elif extends == "repetition":
        exceptions = data.get("exceptions", [])
        pattern = r"\b(\w+)\s+\1\b"
        fields["patterns"] = (_compile(pattern, flags | re.IGNORECASE, path),)
        fields["swaps"] = tuple((w.lower(), "") for w in exceptions)  # reused as the exception list
    return Rule(**fields)


def style_dirs(config: dict) -> list[Path]:
    dirs = [STYLES_DIR / name for name in config.get("styles", [])]
    dirs += [Path(p) for p in config.get("style_paths", [])]
    return dirs


def load_rules(config: "dict | None" = None) -> list[Rule]:
    """Every enabled rule from the configured styles, sorted by id."""
    config = config or load_config()
    disabled = set(config.get("disabled_rules", []))
    rules: list[Rule] = []
    for directory in style_dirs(config):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            rule = parse_rule(path, directory.name)
            if rule.enabled and rule.id not in disabled:
                rules.append(rule)
    return sorted(rules, key=lambda r: r.id)


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


def format_message(message: str, *args) -> str:
    """Fill the ``%s`` slots of *message*; missing args become empty strings."""
    slots = message.count("%s")
    values = tuple(str(a) for a in args[:slots]) + ("",) * max(0, slots - len(args))
    return message % values if slots else message


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _context(text: str, start: int, end: int, chars: int) -> str:
    before = text[max(0, start - chars) : start]
    after = text[end : end + chars]
    return _collapse(f"...{before}{text[start:end]}{after}...")


def _finding(
    rule: Rule, seg: md.Segment, start: int, end: int, message: str, chars: int
) -> Finding:
    return Finding(
        rule=rule.id,
        level=rule.level,
        line=seg.line_of(start),
        span=_collapse(seg.text[start:end])[:SPAN_MAX],
        context=_context(seg.text, start, end, chars),
        message=message,
        scope=seg.kind,
    )


def _check_existence(rule: Rule, segs: list[md.Segment], chars: int) -> list[Finding]:
    out = []
    for seg in segs:
        for pattern in rule.patterns:
            for m in pattern.finditer(seg.text):
                if not m.group(0).strip():
                    continue
                out.append(
                    _finding(
                        rule,
                        seg,
                        m.start(),
                        m.end(),
                        format_message(rule.message, _collapse(m.group(0))),
                        chars,
                    )
                )
    return out


def _check_substitution(rule: Rule, segs: list[md.Segment], chars: int) -> list[Finding]:
    out = []
    for seg in segs:
        for pattern, preferred in rule.swaps:
            for m in pattern.finditer(seg.text):
                out.append(
                    _finding(
                        rule,
                        seg,
                        m.start(),
                        m.end(),
                        format_message(rule.message, _collapse(m.group(0)), preferred),
                        chars,
                    )
                )
    return out


def _check_occurrence(rule: Rule, segs: list[md.Segment], chars: int) -> list[Finding]:
    out = []
    for seg in segs:
        matches = list(rule.token.finditer(seg.text))
        count = len(matches)
        over = rule.max is not None and count > rule.max
        under = rule.min is not None and count < rule.min
        if not (over or under):
            continue
        start, end = (
            (matches[0].start(), matches[0].end()) if matches else (0, min(len(seg.text), 1))
        )
        limit = rule.max if over else rule.min
        out.append(
            _finding(rule, seg, start, end, format_message(rule.message, count, limit), chars)
        )
    return out


def _check_density(rule: Rule, segs: list[md.Segment], chars: int) -> list[Finding]:
    words = sum(md.word_count(seg.text) for seg in segs)
    hits = [(seg, m) for seg in segs for m in rule.token.finditer(seg.text)]
    allowed = max(rule.floor, int(words * rule.rate / rule.per_words))
    if len(hits) <= allowed:
        return []
    seg, m = hits[0]
    per = words // len(hits)
    message = format_message(rule.message, len(hits), words, allowed, per)
    return [_finding(rule, seg, m.start(), m.end(), message, chars)]


def _check_repetition(rule: Rule, segs: list[md.Segment], chars: int) -> list[Finding]:
    exceptions = {w for w, _ in rule.swaps}
    out = []
    for seg in segs:
        for pattern in rule.patterns:
            for m in pattern.finditer(seg.text):
                if m.group(1).lower() in exceptions:
                    continue
                out.append(
                    _finding(
                        rule,
                        seg,
                        m.start(),
                        m.end(),
                        format_message(rule.message, _collapse(m.group(0))),
                        chars,
                    )
                )
    return out


_CHECKS = {
    "existence": _check_existence,
    "substitution": _check_substitution,
    "occurrence": _check_occurrence,
    "density": _check_density,
    "repetition": _check_repetition,
}


def check_text(
    text: str,
    *,
    path: "str | None" = None,
    config: "dict | None" = None,
    rules: "list[Rule] | None" = None,
) -> list[Finding]:
    """Every finding for *text*, sorted by line then severity (highest first)."""
    config = config or load_config()
    rules = load_rules(config) if rules is None else rules
    chars = int(config.get("context_chars", DEFAULT_CONFIG["context_chars"]))
    parsed = md.blocks(text)
    seen: set = set()
    out: list[Finding] = []
    for rule in rules:
        segs: list[md.Segment] = []
        for scope in rule.scopes:
            segs.extend(md.segments(text, scope, parsed))
        for finding in _CHECKS[rule.extends](rule, segs, chars):
            key = (finding.rule, finding.line, finding.span)
            if key in seen:
                continue
            seen.add(key)
            finding.path = path
            out.append(finding)
    return sorted(out, key=lambda f: (f.line, -f.rank, f.rule))


def check_path(path: Path, **kwargs) -> list[Finding]:
    return check_text(Path(path).read_text(encoding="utf-8"), path=str(path), **kwargs)


def filter_level(findings: list[Finding], min_level: str) -> list[Finding]:
    floor = LEVEL_RANK[min_level]
    return [f for f in findings if f.rank >= floor]


def has_level(findings: list[Finding], level: str) -> bool:
    floor = LEVEL_RANK[level]
    return any(f.rank >= floor for f in findings)


def counts(findings: list[Finding]) -> dict[str, int]:
    return {level: sum(1 for f in findings if f.level == level) for level in LEVELS}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def counts_line(findings: list[Finding]) -> str:
    c = counts(findings)
    parts = [
        f"{c[level]} {level}{'s' if c[level] != 1 else ''}"
        for level in reversed(LEVELS)
        if c[level]
    ]
    return f"{len(findings)} finding(s): {', '.join(parts)}" if findings else "clean"


def format_text(findings: list[Finding], path: "str | None" = None) -> str:
    """One line per finding under a header; ``<path>: clean`` when empty."""
    label = path or (findings[0].path if findings and findings[0].path else "text")
    if not findings:
        return f"{label}: clean"
    lines = [f"{label}: {counts_line(findings)}"]
    for f in findings:
        lines.append(f"  - line {f.line} [{f.level}] {f.rule}: {f.message}")
        lines.append(f"      {f.context}")
    return "\n".join(lines)


def format_json(findings: list[Finding]) -> str:
    return json.dumps([f.as_dict() for f in findings], indent=2)


def summary_lines(rules: "list[Rule] | None" = None, min_level: str = "warning") -> list[str]:
    """The rule set as a compact reminder: one line per rule at *min_level* or above."""
    rules = load_rules() if rules is None else rules
    floor = LEVEL_RANK[min_level]
    return [f"- {r.id} [{r.level}]: {r.summary}" for r in rules if r.rank >= floor]
