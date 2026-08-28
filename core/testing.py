"""Shared fixtures for tests that need a git repo and a fake main-apiary.

``core/test_install.py``, ``test_drift.py``, ``test_cascade.py`` and
``test_uninstall.py`` each grew their own copy of the same two helpers: a
``_git_init`` that shells out to ``git init`` plus ``git commit
--allow-empty``, and a ``_make_fake_apiary`` that ``copytree``-d thirteen
top-level directories of the real checkout into a tmpdir. Sixty-seven tests
paid both costs in ``setUp``: ~3.7 MB and several hundred files copied per
test, plus two git subprocesses (review §5 Phase 4, "shared git-repo
fixture").

Two changes make that cheap without weakening isolation:

* **A golden tree, built once per pytest process.** ``golden_apiary()``
  assembles the pieces ``core/install.py`` actually reads — VERSION,
  ``profiles/``, ``context-rules/``, ``migrations/``, the two sentinel
  modules ``core/self_bootstrap.py`` checks for, and every
  ``<tool>/commands/*.md`` — and nothing else. Each test still gets its own
  private copy, so writes stay isolated; the copy is just two orders of
  magnitude smaller.
* **A golden git repo, likewise.** ``init_git_repo()`` copies a prepared
  ``.git`` (one empty commit) instead of spawning ``git init`` and ``git
  commit``. Same resulting repo, no subprocess.

Both goldens live in one ``TemporaryDirectory`` removed at interpreter exit.

:func:`hermetic_env` is unrelated to speed: it is the env-builder the hook
subprocess tests need so a live Claude session's ``APIARY_*`` /
``CLAUDE_PROJECT_DIR`` values cannot leak into the hook under test
(subsystems/core.md §"Hermeticity" (a)).
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

# Standalone files the fake apiary needs at its root.
APIARY_FILES = ("VERSION",)

# Whole directories: small, and read wholesale by install (profiles are
# resolved and merged, context-rules are rendered into CLAUDE.md) or checked
# for existence (migrations, by self_bootstrap's sentinel list and by
# `apiary update`).
APIARY_TREES = ("profiles", "context-rules", "migrations")

# Individual modules that exist only to satisfy an "is this really a
# main-apiary checkout?" test: core/self_bootstrap.py's _SENTINEL_PATHS and
# core/utils/state._looks_like_apiary.
APIARY_SENTINEL_MODULES = ("core/install.py", "core/cli.py")

# Mirrors core.install._slash_command_sources. Only `<tool>/commands/*.md` is
# copied — the rest of each tool tree is never read through the apiary path.
COMMAND_TOOLS = (
    "budgeter",
    "scribe",
    "core",
    "docs",
    "refiner",
    "harden",
    "compass",
    "researcher",
    "runner",
    "incubator",
)

# Env vars a live Claude Code session exports that would otherwise reach a
# hook under test through os.environ.copy(). Prefixes, matched case-sensitively.
_LEAKY_PREFIXES = ("APIARY_",)
_LEAKY_NAMES = ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE")

_session_tmp: tempfile.TemporaryDirectory | None = None
_golden_apiary: Path | None = None
_golden_git: Path | None = None


def _session_root() -> Path:
    """Root of the per-process scratch dir holding the golden trees."""
    global _session_tmp
    if _session_tmp is None:
        _session_tmp = tempfile.TemporaryDirectory(prefix="apiary-fixtures-")
        atexit.register(_session_tmp.cleanup)
    return Path(_session_tmp.name)


def golden_apiary() -> Path:
    """Build (once) and return the template fake main-apiary checkout.

    Never hand this to a test directly — it is shared. Use
    :func:`make_fake_apiary`, which copies it.
    """
    global _golden_apiary
    if _golden_apiary is not None:
        return _golden_apiary

    golden = _session_root() / "golden-apiary"
    golden.mkdir()
    for name in APIARY_FILES:
        shutil.copy2(REPO_ROOT / name, golden / name)
    for name in APIARY_TREES:
        shutil.copytree(REPO_ROOT / name, golden / name)
    for rel in APIARY_SENTINEL_MODULES:
        dest = golden / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dest)
    for tool in COMMAND_TOOLS:
        src = REPO_ROOT / tool / "commands"
        if src.is_dir():
            shutil.copytree(src, golden / tool / "commands")
    _golden_apiary = golden
    return golden


def _golden_git_dir() -> Path:
    """Build (once) and return a ``.git`` with a single empty commit."""
    global _golden_git
    if _golden_git is not None:
        return _golden_git

    seed = _session_root() / "golden-git"
    seed.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=seed, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "init",
        ],
        cwd=seed,
        check=True,
    )
    _golden_git = seed / ".git"
    return _golden_git


def init_git_repo(path: Path) -> Path:
    """Make *path* a git repo with one empty commit already in it.

    Equivalent to ``git init`` + ``git commit --allow-empty``, done by
    copying a prepared ``.git`` instead of spawning two subprocesses. The
    seed repo is created in a temp dir with no absolute paths in its config,
    so the copy is self-contained wherever it lands.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_golden_git_dir(), path / ".git", dirs_exist_ok=True)
    return path


def make_fake_apiary(
    root: Path,
    *,
    name: str = "apiary_copy",
    git: bool = False,
    self_bootstrap: bool = False,
    extra_trees: Sequence[str] = (),
) -> Path:
    """Create a private fake main-apiary under *root* and return its path.

    ``git=True`` makes it a git repo (drift and cascade need main-apiary to
    look like one). ``self_bootstrap=True`` additionally runs
    ``core.self_bootstrap`` so the checkout has its own self-pointer and a
    registry with uid 1 taken, which is what ``core/drift.py`` verifies
    before it will do anything.

    ``extra_trees`` copies whole top-level directories of the real checkout on
    top of the golden. Only tests that *execute* code out of the fake apiary
    need it — the generated launcher resolves ``<main-apiary>/<script>`` at
    run time, so a test that runs `launch.py scribe/notes.py` needs the real
    ``scribe`` and ``core`` trees to be there. Everything else reads the fake
    apiary for data only, and pays nothing.
    """
    fake = Path(root) / name
    shutil.copytree(golden_apiary(), fake)
    for tree in extra_trees:
        shutil.copytree(REPO_ROOT / tree, fake / tree, dirs_exist_ok=True)
    (fake / ".repos").mkdir(exist_ok=True)
    if git:
        init_git_repo(fake)
    if self_bootstrap:
        from core import self_bootstrap as sb  # noqa: PLC0415 — import cost is real

        sb.self_bootstrap(fake)
    return fake


def hermetic_env(**overrides: str) -> dict[str, str]:
    """``os.environ`` with apiary's own variables stripped, plus *overrides*.

    Hook tests run the hook as a subprocess and used to pass
    ``os.environ.copy()``. Inside a live Claude Code session that copy carries
    ``CLAUDE_PROJECT_DIR``, ``APIARY_TARGET_STATE_DIR`` and
    ``APIARY_MAIN_REPO`` pointing at this checkout — so the hook under test
    could resolve real state instead of the tmpdir the test built, and the
    result would differ between a terminal run and a CI run. Everything else
    (PATH, SystemRoot, HOME) is preserved: the subprocess still has to be able
    to start a Python.

    An override with an empty value is dropped rather than set, so callers can
    write ``hermetic_env(APIARY_RUNNER_SUBPROCESS="1" if flag else "")``.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_LEAKY_PREFIXES) and k not in _LEAKY_NAMES
    }
    for key, value in overrides.items():
        if value == "":
            env.pop(key, None)
        else:
            env[key] = value
    return env
