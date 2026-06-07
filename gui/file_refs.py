"""Per-run registry of drag-dropped file *references* (not copies).

pywebview 5.4 surfaces the real absolute path of a dropped file on every
platform — WebView2 via ``CoreWebView2File.Path``, cocoa via the dragging
pasteboard, gtk via ``drag-data-received`` — and stamps it onto the drop
event as ``pywebviewFullPath`` (see ``webview/util.py``). So we don't copy
bytes: we just record where the file already lives and let the composer pass
that path to Claude, who reads the original in place.

Pasted images are the exception. A clipboard bitmap (screenshot, copied image
region) has *no* source path, so the path-not-copy model can't apply: we
materialize the bytes into an *owned* temp file under ``<state-dir>/pasted/``
and register that. Owned entries carry ``owned: True`` so remove/clear/reset
delete the file we created — a dropped reference's target is never touched.

The registry is a JSON list persisted under the GUI state dir. It's wiped on
GUI startup (``reset``), which also deletes the owned pasted files, so a run
never inherits last run's references or temp bytes. For dropped references we
own no file data — only pointers — so clearing them is pure bookkeeping and
can never touch the user's actual files.
"""

from __future__ import annotations

import json
import mimetypes
import shutil
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
        # Owned temp files for pasted clipboard images live alongside the store
        # (so a test store gets an isolated pasted dir too).
        self._pasted_dir = self._store.parent / "pasted"

    @property
    def store(self) -> Path:
        return self._store

    @property
    def pasted_dir(self) -> Path:
        return self._pasted_dir

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
        """Clear the registry and wipe owned (pasted) temp files. Called once on
        GUI startup so a run inherits neither last run's references nor its
        materialized bytes. Dropped references point at the user's own files, so
        only the pasted dir — which we created — is deleted."""
        self._write([])
        try:
            if self._pasted_dir.exists():
                shutil.rmtree(self._pasted_dir, ignore_errors=True)
        except OSError:
            pass

    def _delete_if_owned(self, entry: dict) -> None:
        """Delete an entry's on-disk file only if we created it (``owned``).
        A dropped reference points at a file the user owns — never touch it."""
        if not entry.get("owned"):
            return
        p = entry.get("path")
        if not p:
            return
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass

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
            # A drop is a pointer to the user's file — we did NOT create it, so
            # remove/clear/reset must never delete it. Contrast add_pasted_bytes.
            "owned": False,
        }
        entries.append(entry)
        self._write(entries)
        return {"ok": True, **entry}

    def add_pasted_bytes(
        self, data: bytes, mime: Optional[str] = None, name: Optional[str] = None
    ) -> dict:
        """Materialize a pasted clipboard image into an owned temp file, then
        register it like a drop.

        A clipboard bitmap has no source path, so — unlike :meth:`add`, which
        records a pointer — we write the bytes ourselves under ``pasted_dir``
        and flag the entry ``owned`` so remove/clear/reset delete the file we
        created. Returns ``{"ok": True, ...descriptor}`` or
        ``{"ok": False, "error": ...}``. Never raises.
        """
        if not isinstance(data, (bytes, bytearray)) or not data:
            return {"ok": False, "error": "empty data"}
        # This is the image-paste path; an absent/unknown mime defaults to PNG
        # (overwhelmingly what clipboard bitmaps are) rather than a generic
        # octet-stream, so the temp file gets a sensible .png extension.
        mime = mime if (isinstance(mime, str) and mime) else "image/png"
        ext = mimetypes.guess_extension(mime) or ".png"
        file_id = uuid.uuid4().hex[:8]
        display = name if (isinstance(name, str) and name) else f"pasted-{file_id}{ext}"
        target = self._pasted_dir / f"{file_id}{ext}"
        try:
            self._pasted_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(data))
        except OSError:
            return {"ok": False, "error": "could not write pasted file"}

        entry = {
            "id": file_id,
            "name": display,
            "path": str(target.resolve()),
            "type": mime,
            "size": len(data),
            "added": _now_iso(),
            "announced": False,
            # We created this file — remove/clear/reset delete it.
            "owned": True,
        }
        entries = self._read()
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
        """Drop one reference by id. A dropped file's target is left alone; an
        owned (pasted) temp file is deleted, since we created it."""
        if not isinstance(file_id, str) or not file_id:
            return False
        entries = self._read()
        kept = [e for e in entries if e.get("id") != file_id]
        if len(kept) == len(entries):
            return False
        for e in entries:
            if e.get("id") == file_id:
                self._delete_if_owned(e)
        return self._write(kept)

    def clear(self) -> bool:
        """Drop every reference. Dropped targets are untouched; owned (pasted)
        temp files are deleted."""
        for e in self._read():
            self._delete_if_owned(e)
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
