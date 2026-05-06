"""``apiary self-bootstrap`` — first-machine setup of main-apiary.

Per MIGRATION-PLAN.md §7.9, fresh-machine setup needs to:

1. Verify cwd is a real apiary checkout (sentinel files present).
2. Initialize ``<main-apiary>/.repos/registry.json`` if absent.
3. Initialize ``<main-apiary>/.repos/next_id`` if absent.
4. Run the equivalent of ``apiary install --target <main-apiary>`` on
   main-apiary itself, since it's a bootstrapped repo from its own POV (D3).

The function is idempotent: re-running on an already-bootstrapped main-apiary
just refreshes the install (same as ``apiary install --target <main-apiary>``).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import install as install_mod
from core.utils import state

# Files/dirs that uniquely identify a path as a main-apiary checkout. A
# "fake apiary" tmpdir for tests can satisfy these by copying the real
# files in.
_SENTINEL_PATHS = (
    "core/apiary_bootstrap.py",
    "core/install.py",
    "migrations",
    "VERSION",
    "profiles",
)


class SelfBootstrapError(Exception):
    """Raised when self-bootstrap can't complete (bad cwd, missing pieces)."""


def self_bootstrap(apiary_repo: Path | None = None) -> install_mod.InstallResult:
    """Bootstrap main-apiary against itself.

    *apiary_repo* defaults to the script's own repo root — useful in tests
    that need to target a fake apiary tmpdir. In production the caller
    invokes this from inside main-apiary, so the default is what they want.
    """
    apiary = (Path(apiary_repo) if apiary_repo else REPO_ROOT).resolve()
    _verify_apiary_checkout(apiary)
    _ensure_registry_initialized(apiary)
    return install_mod.install(apiary, apiary_repo=apiary)


def _verify_apiary_checkout(apiary: Path) -> None:
    missing = [p for p in _SENTINEL_PATHS if not (apiary / p).exists()]
    if missing:
        raise SelfBootstrapError(
            f"{apiary} does not look like a main-apiary checkout — "
            f"missing: {', '.join(missing)}. Run from the apiary repo root, "
            "or pass --apiary-repo explicitly."
        )


def _ensure_registry_initialized(apiary: Path) -> None:
    """Create empty registry / next_id files when they're missing.

    The first ``allocate_next_id`` call already creates ``next_id``
    (initialized to 1) under FileLock, and ``apiary install`` writes the
    registry on its first registration. So this function only matters as
    an explicit "fresh machine" guard — if either file is missing, we
    create empty placeholders so the install path doesn't have to special-
    case it.
    """
    repos = state.repos_dir(apiary)
    repos.mkdir(parents=True, exist_ok=True)

    registry = state.registry_path(apiary)
    if not registry.is_file():
        registry.write_text("{}\n", encoding="utf-8")

    next_id = state.next_id_path(apiary)
    if not next_id.is_file():
        next_id.write_text("0\n", encoding="utf-8")
