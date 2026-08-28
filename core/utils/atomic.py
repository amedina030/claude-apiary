"""Atomic file writes — write a sibling temp file, then ``os.replace``.

``os.replace`` is atomic on POSIX and on Windows (``MoveFileEx`` with
``MOVEFILE_REPLACE_EXISTING``), so a concurrent reader sees either the
previous file or the new one, never a half-written mix, and a process
killed mid-write leaves the old content intact.

The temp file is created in the target's own directory — same filesystem,
so the replace cannot fail with ``EXDEV`` — and is named
``<target-name>.<random>.tmp`` so orphans left by a hard kill are
recognisable (``budgeter.lib.logger.cleanup_session`` sweeps them by that
glob). It is unlinked if anything fails before the replace.

This is the one copy. Phase 3.2 folded in the last hand-rolled
``<name>.tmp`` writers — ``core/utils/state.py`` (×4), ``core/install.py``
(×2), ``core/hooks_lib.save_settings`` (which was not even atomic),
``scribe/store.py``, ``runner/executor.py`` and ``gui/tabs_state.py`` — so
there is no eighth. The read half lives next door in ``core.utils.jsonio``.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path

# os.replace onto a file another process has open raises PermissionError on
# Windows; readers hold files for milliseconds, so a few short retries turn
# a lost write into a delayed one.
_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF_SECONDS = 0.05


def _replace_with_retry(tmp_path: str, target: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp_path, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))


def _match_mode(tmp_path: str, target: Path) -> None:
    """mkstemp creates 0600 files; give the temp the target's mode (or a
    normal 0644) so an atomic rewrite does not silently lock the file down."""
    try:
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        os.chmod(tmp_path, mode)
    except OSError:
        pass


def replace_atomic(src: Path | str, dst: Path | str) -> None:
    """Move *src* onto *dst* atomically, creating ``dst``'s parent.

    The move half of this module: same ``os.replace``, same Windows retry as
    :func:`write_text_atomic`. Use it instead of copy-then-unlink when the
    file is *moving* (scribe archiving a note body) — a copy that dies
    between the two steps leaves two files, a replace cannot.
    """
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    _replace_with_retry(str(src), target)


def write_text_atomic(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically, creating parent directories."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=target.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
        _match_mode(tmp_path, target)
        _replace_with_retry(tmp_path, target)
    except BaseException:
        # Includes KeyboardInterrupt/SystemExit: never leave the temp behind.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_json_atomic(
    path: Path | str,
    data,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    trailing_newline: bool = False,
) -> None:
    """Serialize *data* as JSON and write it to *path* atomically."""
    text = json.dumps(data, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        text += "\n"
    write_text_atomic(path, text)
