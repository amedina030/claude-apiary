"""Aggregate scribe notes across registered apiary repos.

Reads ``<state-dir>/scribe/<type>/<year>/index.jsonl`` directly (faster than
shelling out to ``notes.py list``). Each target repo's state-dir is resolved
via the per-target pointer at ``<target>/.apiary/pointer`` written at
registration time. Unmigrated targets fall back to the legacy in-repo
``<target>/.apiary/scribe/`` path so old data still surfaces during the
migration window.

Returns active notes only — archived notes are filtered out. Includes the
body file path so the frontend can lazy-load full content on click.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# core.utils.state lives one directory up; mirror the import-path pattern
# used by other gui modules (sys.path insert + repo-root-relative import).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.utils.state import resolve_state_dir  # noqa: E402


@dataclass
class NoteEntry:
    repo: str            # absolute path of the repo this note lives in
    repo_label: str      # display label (basename) for grouping
    type: str            # "todo" | "decision" | etc — derived from folder name (singularized)
    folder: str          # raw scribe subfolder name (e.g. "todos", "decisions")
    display_id: str
    summary: str
    brief_summary: str   # one-sentence headline for sidebar display (may be "")
    status: str
    timestamp: str
    has_body: bool
    body_path: str       # absolute path to the .md body file, "" if no body

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "repo_label": self.repo_label,
            "type": self.type,
            "folder": self.folder,
            "display_id": self.display_id,
            "summary": self.summary,
            "brief_summary": self.brief_summary,
            "status": self.status,
            "timestamp": self.timestamp,
            "has_body": self.has_body,
            "body_path": self.body_path,
        }


# Singularize the folder name into a human "type". Scribe uses pluralized folder
# names for some types (todos, decisions, handoffs, references, blockers,
# wishlists, learnings) and singular for others (context, general, memory).
_FOLDER_TO_TYPE = {
    "todos": "todo",
    "decisions": "decision",
    "handoffs": "handoff",
    "references": "reference",
    "blockers": "blocker",
    "wishlists": "wishlist",
    "learnings": "learning",
    "context": "context",
    "general": "general",
}

# Folders to skip entirely.
_SKIP_FOLDERS = {"memory", "backup", "backups", "archive"}


def _resolve_scribe_root(repo: Path) -> Optional[Path]:
    """Find this target's scribe state directory, or None.

    ``resolve_state_dir`` owns the precedence (pins →
    ``.apiary/pointer`` breadcrumb → in-repo ``.apiary/``). Two arguments
    matter here and nowhere else:

    * ``use_env=False`` — the aggregator scans *several* repos in one
      process, so the launcher's ``APIARY_TARGET_STATE_DIR`` (which names
      whichever repo started the GUI) would hand every repo the same
      answer.
    * ``require_exists=True`` — a repo with no notes yet is "nothing to
      show", not an empty directory to create.
    """
    return resolve_state_dir(
        repo=repo, subdir="scribe", use_env=False, require_exists=True,
    )


def _scan_repo(repo: Path) -> list[NoteEntry]:
    scribe_root = _resolve_scribe_root(repo)
    if scribe_root is None:
        return []
    out: list[NoteEntry] = []
    repo_label = repo.name
    repo_str = str(repo)
    for type_dir in scribe_root.iterdir():
        if not type_dir.is_dir():
            continue
        folder = type_dir.name
        if folder in _SKIP_FOLDERS:
            continue
        type_name = _FOLDER_TO_TYPE.get(folder, folder.rstrip("s"))
        for year_dir in type_dir.iterdir():
            if not year_dir.is_dir() or year_dir.name == "archive":
                continue
            index = year_dir / "index.jsonl"
            if not index.is_file():
                continue
            try:
                with index.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        status = rec.get("status", "")
                        if status not in ("active", "open", "deferred", ""):
                            # Drop archived/done/killed entries from the sidebar.
                            if status in ("archived", "done", "completed", "killed", "closed"):
                                continue
                        seq = rec.get("seq")
                        body_path = ""
                        has_body = bool(rec.get("has_body"))
                        if has_body and seq is not None:
                            cand = year_dir / f"{seq}.md"
                            if cand.is_file():
                                body_path = str(cand)
                        out.append(
                            NoteEntry(
                                repo=repo_str,
                                repo_label=repo_label,
                                type=type_name,
                                folder=folder,
                                display_id=rec.get("display_id", ""),
                                summary=rec.get("summary", ""),
                                brief_summary=rec.get("brief_summary", ""),
                                status=status,
                                timestamp=rec.get("timestamp", ""),
                                has_body=has_body,
                                body_path=body_path,
                            )
                        )
            except OSError:
                continue
    return out


def aggregate(repos: list[Path]) -> tuple[list[NoteEntry], list[str]]:
    """Aggregate notes from all repos. Returns (notes, warnings)."""
    notes: list[NoteEntry] = []
    warnings: list[str] = []
    for r in repos:
        try:
            notes.extend(_scan_repo(r))
        except Exception as e:
            warnings.append(f"{r}: {e}")
    # Newest first, by timestamp string (ISO-8601 sorts lexicographically).
    notes.sort(key=lambda n: n.timestamp, reverse=True)
    return notes, warnings


def read_body(body_path: str) -> str:
    if not body_path:
        return ""
    p = Path(body_path)
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"(could not read body: {e})"


class ScribeAggregatorService:
    """Periodically aggregate notes from registered repos and push to the frontend.

    V1 polls on a 5-second tick; we don't wire watchdog file events here to keep
    the dependency surface tight (watchdog is wired in for theme.json hot-reload
    in Phase 4 — adding it for index.jsonl events is a nice-to-have, not load-bearing).
    """

    def __init__(
        self,
        repos: list[Path],
        on_update: Callable[[list[NoteEntry], list[str]], None],
        interval: float = 5.0,
    ) -> None:
        self._repos = list(repos)
        self._on_update = on_update
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def update_repos(self, repos: list[Path]) -> None:
        self._repos = list(repos)

    def start(self) -> None:
        # Push once synchronously so the sidebar populates immediately.
        try:
            notes, warnings = aggregate(self._repos)
            self._on_update(notes, warnings)
        except Exception:
            pass
        self._thread = threading.Thread(target=self._run, name="scribe-aggregator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                notes, warnings = aggregate(self._repos)
                self._on_update(notes, warnings)
            except Exception:
                continue
