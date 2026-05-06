"""No-op example migration. Reference shape only — does nothing.

What changes: nothing. This file exists so future migration authors have a
working template to copy.
Idempotent: yes (a no-op is trivially idempotent).
"""
from __future__ import annotations

from pathlib import Path

FROM_VERSION = "0.0.0"
TO_VERSION = "0.1.0"


def upgrade(repo_path: Path) -> None:
    """Apply the migration to *repo_path*.

    *repo_path* is the absolute path to a bootstrapped repo being upgraded
    (NOT main-apiary). The migration runner has already verified that the
    repo's pinned version equals ``FROM_VERSION``.

    Must be idempotent and atomic. Raise on failure — the caller will revert
    ``<repo_path>/.claude/apiary/version.json`` to FROM_VERSION and abort
    the chain. See ``migrations/README.md`` for the full contract.
    """
    return None
