"""Repo registry — which apiary repos to scan for scribe notes.

Config file: `~/.claude/apiary_repos.json` (a flat JSON list of absolute repo paths).
Auto-seeded on first run with: the apiary repo itself + any unique repo cwds we
can pull from `~/.claude/.session-history.json`.

The user can hand-edit the file to add or remove repos. On malformed JSON the
in-memory list falls back to the auto-seeded default and a toast is surfaced.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


CONFIG_PATH = Path.home() / ".claude" / "apiary_repos.json"
SESSION_HISTORY = Path.home() / ".claude" / ".session-history.json"
APIARY_LAUNCH = Path.home() / ".claude" / "apiary_launch.py"


def _print_apiary_repo_path() -> Optional[Path]:
    if not APIARY_LAUNCH.is_file():
        return None
    try:
        out = subprocess.check_output(
            [sys.executable, str(APIARY_LAUNCH), "--print-repo-path"],
            text=True,
            timeout=5,
        ).strip()
        if not out:
            return None
        p = Path(out)
        return p if p.is_dir() else None
    except Exception:
        return None


def _peek_transcript_cwd(transcript_path: Path) -> Optional[Path]:
    """Open a JSONL transcript and pull a `cwd` from the first record that has one."""
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = rec.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return Path(cwd)
    except OSError:
        return None
    return None


def _seed_from_session_history() -> list[Path]:
    """Best-effort: walk recent transcripts in session-history and collect unique cwds."""
    if not SESSION_HISTORY.is_file():
        return []
    try:
        data = json.loads(SESSION_HISTORY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    seen: set[Path] = set()
    out: list[Path] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        tp = entry.get("transcript_path")
        if not isinstance(tp, str):
            continue
        cwd = _peek_transcript_cwd(Path(tp))
        if cwd is None:
            continue
        try:
            resolved = cwd.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        if not resolved.is_dir():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def auto_seed() -> list[Path]:
    """Build the default repo list: apiary repo + any session-history-derived cwds."""
    seeded: list[Path] = []
    seen: set[Path] = set()
    apiary = _print_apiary_repo_path()
    if apiary is not None:
        try:
            apiary = apiary.resolve()
        except OSError:
            pass
        seen.add(apiary)
        seeded.append(apiary)
    for p in _seed_from_session_history():
        if p in seen:
            continue
        seen.add(p)
        seeded.append(p)
    return seeded


def load(config_path: Path = CONFIG_PATH) -> tuple[list[Path], Optional[str]]:
    """Return (repos, error_message). On malformed JSON, returns auto_seed() and an error.
    On missing file, auto-seeds, writes the file, and returns the result.
    """
    if not config_path.is_file():
        seeded = auto_seed()
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps([str(p) for p in seeded], indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
        return seeded, None

    try:
        text = config_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as e:
        return auto_seed(), f"apiary_repos.json invalid: {e}"

    if not isinstance(data, list):
        return auto_seed(), "apiary_repos.json must be a JSON array of paths"

    out: list[Path] = []
    for item in data:
        if not isinstance(item, str):
            continue
        p = Path(item)
        if p.is_dir():
            out.append(p)
    return out, None
