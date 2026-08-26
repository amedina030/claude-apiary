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

This is the one copy: ``compass/synthesize.py``, ``budgeter/lib/logger.py``
and ``budgeter/tune.py`` all call it. Other tmp+replace blocks still live
in ``scribe/store.py``, ``core/utils/state.py``, ``runner/executor.py`` and
``gui/permission_mcp.py`` — folding those in is review Phase 3.2.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_text_atomic(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically, creating parent directories."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
        os.replace(tmp_path, target)
    except BaseException:
        # Includes KeyboardInterrupt/SystemExit: never leave the temp behind.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path | str, data, *, indent: int | None = None,
                      sort_keys: bool = False,
                      trailing_newline: bool = False) -> None:
    """Serialize *data* as JSON and write it to *path* atomically."""
    text = json.dumps(data, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        text += "\n"
    write_text_atomic(path, text)
