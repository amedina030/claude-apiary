"""Per-run registry of drag-dropped file *references* (not copies).

pywebview 5.4 surfaces the real absolute path of a dropped file on every
platform — WebView2 via ``CoreWebView2File.Path``, cocoa via the dragging
pasteboard, gtk via ``drag-data-received`` — and stamps it onto the drop
event as ``pywebviewFullPath`` (see ``webview/util.py``). So we don't copy
bytes: we just record where the file already lives and let the composer pass
that path to Claude, who reads the original in place.

The registry is a JSON list persisted under the GUI state dir. It's wiped on
GUI startup (``reset``) so a run never inherits last run's references. Because
we own no file data — only pointers — clearing is pure bookkeeping and can
never touch the user's actual files.
"""

from __future__ import annotations

import json
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from gui import paths


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FileRefs:
    """JSON-backed list of referenced files for the current GUI run."""

    def __init__(self, store: Optional[Path] = None) -> None:
        self._store = store if store is not None else (paths.state_dir() / "file_refs.json")

    @property
    def store(self) -> Path:
        return self._store

    # --- persistence ---------------------------------------------------------

    def _read(self) -> list[dict]:
        try:
            raw = self._store.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, entries: list[dict]) -> bool:
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            self._store.write_text(
                json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return True
        except OSError:
            return False

    def reset(self) -> None:
        """Clear the registry. Called once on GUI startup."""
        self._write([])

    # --- mutations -----------------------------------------------------------

    def add(self, path: str) -> dict:
        """Record a reference to an existing file at ``path``.

        Returns ``{"ok": True, ...descriptor}`` or ``{"ok": False, "error": ...}``.
        Idempotent: dropping the same path twice returns the existing entry
        rather than duplicating it. Never raises.
        """
        if not isinstance(path, str) or not path:
            return {"ok": False, "error": "invalid path"}
        try:
            p = Path(path).resolve()
        except (OSError, ValueError):
            return {"ok": False, "error": "could not resolve path"}
        if not p.is_file():
            return {"ok": False, "error": "not a file"}

        entries = self._read()
        resolved = str(p)
        for e in entries:
            if e.get("path") == resolved:
                return {"ok": True, **e}  # already referenced

        mime, _ = mimetypes.guess_type(p.name)
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        entry = {
            "id": uuid.uuid4().hex[:8],
            "name": p.name,
            "path": resolved,
            "type": mime or "application/octet-stream",
            "size": size,
            "added": _now_iso(),
            # Shared-with-claude marker. Owned here (not in JS) so the manifest
            # can be built authoritatively at send time — see manifest_and_mark.
            "announced": False,
        }
        entries.append(entry)
        self._write(entries)
        return {"ok": True, **entry}

    def manifest_and_mark(self) -> dict:
        """Build the outgoing attach-manifest and mark the listed files shared.

        This is the authoritative send-time path (option B): existence is
        re-checked HERE, the moment the user sends, so a reference whose target
        vanished since it was dropped is dropped from the manifest rather than
        shipping a dead path the assistant can't Read. Files already announced
        on a prior turn are skipped (their paths are in the conversation
        history). The files that make the cut are flagged ``announced`` so they
        aren't re-listed next turn.

        Returns ``{"text": <manifest or "">, "files": <list() snapshot>}`` so
        the frontend can append the text and re-render the panel (dimmed rows,
        live missing flags) in one round-trip. Never raises.
        """
        entries = self._read()
        fresh: list[dict] = []
        changed = False
        for e in entries:
            p = e.get("path")
            if not p or not Path(p).is_file():
                continue  # missing target — never ship a path that can't be Read
            if e.get("announced"):
                continue
            fresh.append(e)
            e["announced"] = True
            changed = True
        if changed:
            self._write(entries)
        if fresh:
            lines = [
                f"- {e['path']} ({e.get('type', 'application/octet-stream')})"
                for e in fresh
            ]
            text = "[attached files — read these with the Read tool:]\n" + "\n".join(lines)
        else:
            text = ""
        return {"text": text, "files": self.list()}

    def remove(self, file_id: str) -> bool:
        """Drop one reference by id. The on-disk file is never touched."""
        if not isinstance(file_id, str) or not file_id:
            return False
        entries = self._read()
        kept = [e for e in entries if e.get("id") != file_id]
        if len(kept) == len(entries):
            return False
        return self._write(kept)

    def clear(self) -> bool:
        """Drop every reference (the user's files are untouched)."""
        return self._write([])

    # --- queries -------------------------------------------------------------

    def list(self) -> list[dict]:
        """All references, each annotated with a live ``exists`` flag so the UI
        can flag a reference whose target was moved or deleted since it was
        added (the path-not-copy tradeoff)."""
        out = []
        for e in self._read():
            entry = dict(e)
            p = entry.get("path")
            entry["exists"] = bool(p) and Path(p).is_file()
            out.append(entry)
        return out
