"""Frozen public API for external tools (parity spec §6).

An external tool (e.g. the standalone Asana tool) imports **this module** to
drive apiary scribe: resolve a target's store by name or path, then read/write
notes through the documented surface. The internal storage layout
(`<apiary>/.repos/<name>-<id>/scribe/`) is deliberately NOT part of the
contract — resolve via :func:`resolve_scribe_dir` / :func:`open_store`, never
by constructing that path yourself. The layout may change behind this function.

Stability contract: the names and signatures in ``__all__`` are frozen. Returned
note dicts are normalized via :func:`normalize_entry` so on-disk representation
(compact JSON, omitted empty lists, implicit ``archived``) is irrelevant to
consumers.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

# Repo-root on path so `core.*` resolves when imported from an external tool.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.utils.state import find_state_dir, registry_path, repos_dir, resolve_apiary_repo
from core.utils.timeutil import parse_iso
from scribe.store import ScribeStore

__all__ = [
    "resolve_scribe_dir",
    "open_store",
    "normalize_entry",
    "parse_display_id",
    "parse_ts",
    "PREFIX_TO_TYPE",
    "TYPE_TO_PREFIX",
    "STATUSES",
]

SCRIBE_SUBDIR = "scribe"
STATUSES = ("active", "done", "dropped", "deferred")

# Prefix <-> type. No `K`/ticket — apiary holds todo-mirrors only (spec F2).
TYPE_TO_PREFIX = {
    "todo": "T",
    "handoff": "H",
    "decision": "D",
    "wishlist": "W",
    "reference": "R",
    "blocker": "B",
    "context": "C",
    "general": "G",
    "learning": "L",
}
PREFIX_TO_TYPE = {v: k for k, v in TYPE_TO_PREFIX.items()}

_DISPLAY_ID_RE = re.compile(r"^([A-Za-z])-(\d{4})-(\d+)$")


def parse_ts(ts: str) -> "datetime | None":
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``.

    Returns a timezone-aware ``datetime`` or ``None`` on any failure. The
    Asana tool uses this to compare scribe timestamps against Asana's
    ``...Z`` timestamps.

    The name is frozen (it is in ``__all__``); the implementation is
    ``core.utils.timeutil.parse_iso``, the one copy in the tree.
    """
    return parse_iso(ts)


def parse_display_id(display_id: str) -> "tuple[str, int, int]":
    """Parse ``PREFIX-YEAR-SEQ`` → ``(type_name, year, seq)``.

    Uppercases the prefix. Raises ``ValueError`` naming the expected form on a
    malformed id or an unknown prefix (e.g. ``K-2026-5`` — apiary has no ``K``).
    """
    if not isinstance(display_id, str):
        raise ValueError("display id must be a string of form TYPE-YEAR-SEQ (e.g. T-2026-1)")
    m = _DISPLAY_ID_RE.match(display_id.strip())
    if not m:
        raise ValueError(
            f"malformed display id {display_id!r}; expected TYPE-YEAR-SEQ (e.g. T-2026-1)"
        )
    prefix = m.group(1).upper()
    note_type = PREFIX_TO_TYPE.get(prefix)
    if note_type is None:
        raise ValueError(
            f"unknown prefix {prefix!r} in {display_id!r}; expected one of {''.join(PREFIX_TO_TYPE)}"
        )
    return note_type, int(m.group(2)), int(m.group(3))


def _registry(apiary_repo: Path) -> dict:
    """Read ``<apiary>/.repos/registry.json``; ``{}`` if missing/malformed."""
    p = registry_path(apiary_repo)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _folder_by_name(apiary_repo: Path, name: str) -> str:
    """Return the ``<name>-<id>`` folder for a registered target *name*.

    Raises ``KeyError`` when no target carries that name, ``ValueError`` when
    more than one does (ambiguous — caller should pass the repo path instead).
    """
    matches = [
        (id_str, entry)
        for id_str, entry in _registry(apiary_repo).items()
        if isinstance(entry, dict) and entry.get("name") == name
    ]
    if not matches:
        raise KeyError(f"no registered target named {name!r}")
    if len(matches) > 1:
        cands = ", ".join(f"{e['name']}-{i}" for i, e in matches)
        raise ValueError(
            f"ambiguous target name {name!r}; candidates: {cands} — pass the repo path instead"
        )
    id_str, _ = matches[0]
    return f"{name}-{id_str}"


def _folder_by_real_path(apiary_repo: Path, repo_path: Path) -> "str | None":
    """Return the ``<name>-<id>`` folder for a registered target *repo_path*."""
    target = str(Path(repo_path).resolve())
    for id_str, entry in _registry(apiary_repo).items():
        if isinstance(entry, dict) and entry.get("real_path") == target:
            return f"{entry['name']}-{id_str}"
    return None


def resolve_scribe_dir(target, *, apiary_repo=None) -> Path:
    """Resolve *target* to its scribe state directory.

    *target* may be either a registered target **name** (e.g. ``"claude-apiary"``)
    or a path to a target **repo** (an existing directory). Returns
    ``<apiary>/.repos/<name>-<id>/scribe/`` — the root a :class:`ScribeStore`
    binds to. The returned path may not exist yet; :func:`open_store` creates it.

    Raises ``KeyError`` when the target is not registered, ``ValueError`` when a
    bare name is ambiguous, ``RuntimeError`` when the apiary repo can't be found.
    """
    apiary = resolve_apiary_repo(apiary_repo)
    cand = Path(str(target))
    if cand.is_dir():
        state_dir = find_state_dir(cand)
        if state_dir is not None:
            return state_dir / SCRIBE_SUBDIR
        folder = _folder_by_real_path(apiary, cand)
        if folder is None:
            raise KeyError(f"target repo not registered: {target}")
        return repos_dir(apiary) / folder / SCRIBE_SUBDIR
    folder = _folder_by_name(apiary, str(target))
    return repos_dir(apiary) / folder / SCRIBE_SUBDIR


def open_store(target, *, apiary_repo=None) -> ScribeStore:
    """Return a :class:`ScribeStore` bound to *target*'s scribe state dir.

    Convenience over :func:`resolve_scribe_dir` — constructs the store (which
    creates the layout on first use). This replaces the source tool's
    ``Store(root=<repo>)``, which assumed per-repo ``.scribe/``.
    """
    return ScribeStore(resolve_scribe_dir(target, apiary_repo=apiary_repo))


def normalize_entry(entry: dict) -> dict:
    """Return the stable, contract-shaped view of a raw index entry.

    Synthesizes fields apiary stores implicitly or omits, so consumers never
    depend on on-disk representation:
      - ``tags`` / ``areas`` → always a list (empty if absent)
      - ``auto_generated`` → bool (default ``False``)
      - ``archived`` → bool (``True`` iff ``archived_at`` present or the store
        tagged the row ``_from_archive``)
      - ``status_changed_at`` → str or ``None``
    """
    return {
        "display_id": entry.get("display_id"),
        "type": entry.get("type"),
        "year": entry.get("year"),
        "seq": entry.get("seq"),
        "status": entry.get("status", "active"),
        "status_changed_at": entry.get("status_changed_at"),
        "session": entry.get("session"),
        "timestamp": entry.get("timestamp"),
        "summary": entry.get("summary", ""),
        "brief_summary": entry.get("brief_summary", ""),
        "tags": list(entry.get("tags") or []),
        "areas": list(entry.get("areas") or []),
        "auto_generated": bool(entry.get("auto_generated", False)),
        "archived": bool(entry.get("archived_at") or entry.get("_from_archive")),
        "has_body": bool(entry.get("has_body", False)),
        "role": entry.get("role"),
        "mission": entry.get("mission"),
        "supersedes": entry.get("supersedes"),
    }
