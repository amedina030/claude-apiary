"""Tolerant JSON reading — the read half of ``core.utils.atomic``'s writes.

Apiary's on-disk state is a scatter of small JSON files (registry, pins,
pointers, settings) written by hooks that can be killed mid-run. Every
reader wants the same contract: *give me a dict, or tell me there isn't
one*. That block was copy-pasted five times (review finding X-3); this is
the one copy.

Writing lives next door in ``core.utils.atomic``
(``write_json_atomic`` / ``write_text_atomic``).
"""
from __future__ import annotations

import json
from pathlib import Path


def read_json_object(path: Path | str) -> dict | None:
    """Load a JSON **object** from *path*, or return ``None``.

    ``None`` means "no usable object here", for every reason a caller
    cannot act on differently anyway: the file is missing, it is not
    readable, it is not valid JSON, or its top level is not an object
    (a bare list or scalar). Never raises.

    Callers that treat "absent" as "empty" should write
    ``read_json_object(p) or {}``.
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError covers json.JSONDecodeError and a UnicodeDecodeError
        # from a binary file that happens to sit at this path.
        return None
    return data if isinstance(data, dict) else None
