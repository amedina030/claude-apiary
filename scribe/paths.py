"""Where scribe's state lives, and who is writing to it.

Two questions the CLI, the installer, the startup banner and the backup tool
all ask, kept out of ``notes.py`` so none of them has to import a CLI module
to get an answer.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.utils.project import get_project_key
from core.utils.state import resolve_state_dir

#: Pre-migration state root. Nothing writes here any more; the backup CLI and
#: the startup banner still fall back to it for a target that never migrated.
CLAUDE_DIR = Path.home() / '.claude'
PROJECTS_DIR = CLAUDE_DIR / 'projects'

#: The per-target state layout (C-2026-46): scribe reads and writes under
#: ``<state-dir>/scribe/``, where ``<state-dir>`` is the registry-allocated
#: folder at ``<apiary>/.repos/<name>-<id>/``. The launcher exports
#: APIARY_TARGET_STATE_DIR once the registry resolver has run; without it,
#: :func:`scribe_state_dir` falls back to ``<git-root>/.apiary/scribe/``.
APIARY_STATE_DIRNAME = '.apiary'
SCRIBE_SUBDIR = 'scribe'

#: A project key is a directory name under PROJECTS_DIR; anything with a path
#: separator or a traversal component is rejected before it is joined.
_PROJECT_KEY_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,200}$')


class ProjectKeyError(ValueError):
    """A ``--project`` override that would escape the projects directory."""


def scribe_state_dir(start: "Path | None" = None) -> "Path | None":
    """The scribe state *directory* under the active layout.

    Delegates to :func:`core.utils.state.resolve_state_dir` — the one place
    the precedence lives (launcher env var → the repo's pins → the legacy
    ``.apiary/pointer`` breadcrumb → ``<repo>/.apiary/scribe/``). Returns
    ``None`` when *start* is not inside a git repo at all: scribe has no cwd
    fallback, because writing notes into whatever directory the shell happened
    to be in is worse than saying "I don't know where they go".
    """
    return resolve_state_dir(start, subdir=SCRIBE_SUBDIR)


def project_key(override: "str | None" = None) -> str:
    """The project key, from a ``--project`` override or the cwd.

    Raises :class:`ProjectKeyError` for an override that contains anything but
    the characters a project-key directory name may hold, or that resolves
    outside PROJECTS_DIR. Both checks, because the character class is the rule
    and the resolve is the proof.
    """
    if not override:
        return get_project_key(Path.cwd())
    if not _PROJECT_KEY_RE.match(override):
        raise ProjectKeyError(
            f'--project value contains invalid characters: {override!r}')
    resolved = (PROJECTS_DIR / override).resolve()
    if not str(resolved).startswith(str(PROJECTS_DIR.resolve())):
        raise ProjectKeyError(
            f'--project value escapes projects directory: {override!r}')
    return override


def resolve_store_dir(override: "str | None" = None,
                      start: "Path | None" = None) -> Path:
    """The scribe dir to bind a store to, falling back to the legacy path.

    Raises :class:`ProjectKeyError` on a bad ``--project`` override.
    """
    return scribe_state_dir(start) or (PROJECTS_DIR / project_key(override))


def session_identity() -> tuple:
    """``(role, mission, session_id)`` for the current session, or empty strings.

    Fail-soft on purpose: a missing or malformed identity file means the note
    is written without role/mission, not that the CLI refuses to run.
    """
    try:
        from core.session import load_identity
        identity = load_identity()
        return (identity.get('role', ''), identity.get('mission', ''),
                identity.get('session_id', ''))
    except Exception:
        return ('', '', '')
