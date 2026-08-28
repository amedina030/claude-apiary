"""Repo registry — which apiary repos to scan for scribe notes.

Source of truth: ``<main-apiary>/.repos/registry.json``. After the per-repo
migration (2026-05) the GUI reads the registry directly instead of the
hand-curated ``~/.claude/apiary_repos.json`` it used pre-migration.

The list of repos to surface in the GUI sidebar = registered repos whose
``real_path`` is currently a directory on disk. Operators can hide a repo
from the sidebar by uninstalling apiary from it
(``poetry run apiary uninstall --target <repo>``); there's no separate
GUI-only filter file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from gui.paths import main_apiary


def _registry_path() -> Path:
    # Resolved per call, not at import: main_apiary() answers differently
    # for source and frozen builds, and a module-level constant here used
    # to bake in this file's grandparent — which in a PyInstaller bundle is
    # <bundle>/_internal, where no registry exists (#T-2026-248).
    return main_apiary() / ".repos" / "registry.json"


def load() -> tuple[list[Path], Optional[str]]:
    """Return ``(repos, error)`` where *repos* is the list of bootstrapped
    repos to surface in the GUI sidebar.

    Errors when:
    - the registry file is missing (`error` describes the situation),
    - the registry is malformed (`error` describes the JSON parse).
    Returns the best-effort list anyway so the GUI can still open with an
    empty sidebar.
    """
    p = _registry_path()
    if not p.is_file():
        return [], f"registry not found at {p}; run `apiary self-bootstrap` first"

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"registry malformed: {exc}"

    if not isinstance(data, dict):
        return [], "registry is not a JSON object"

    out: list[Path] = []
    seen: set[Path] = set()
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        real = entry.get("real_path")
        if not isinstance(real, str) or not real:
            continue
        candidate = Path(real)
        if not candidate.is_dir():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(candidate)
    return out, None
