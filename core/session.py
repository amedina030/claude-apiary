"""
Session identity helpers — shared by hooks, scribe, and startup.

Where session state lives (apiary writes nothing under ``~/.claude``):

* identity files ``identity-<short>.json`` ({role, mission, registered,
  wants_role, wants_mission}) and the session history / last-session
  records: ``<state-dir>/sessions/`` — the per-target dir the launcher
  exports as ``APIARY_TARGET_STATE_DIR`` (``<main-apiary>/.repos/<slug>/``);
* once-per-session hook flag files ``<uuid>_<suffix>``:
  ``<repo>/.claude/apiary/session-tmp/`` (created by the installer,
  git-ignored).

Until 2026-08 both went to ``~/.claude`` (review S1: ~4,000 stray files,
growing ~5 per session). When neither a repo nor a state dir can be
resolved the fallback is a directory under the OS temp dir, never the home
directory.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

SESSION_TMP_DIRNAME = "session-tmp"
SESSIONS_DIRNAME = "sessions"


HISTORY_SCHEMA_VERSION = 1


def load_history(path: Path) -> list:
    """Read the session history file in its v1 shape
    (``{"schema_version": 1, "sessions": [...]}``); a bare list (the old
    ``~/.claude/.session-history.json`` ring buffer) is accepted too.
    Anything unreadable or malformed reads as empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("sessions", [])
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def dump_history(entries: list) -> str:
    return json.dumps({"schema_version": HISTORY_SCHEMA_VERSION, "sessions": list(entries)}, indent=2)


_warned_fallback = False


def _fallback_root(warn: bool = True) -> Path:
    """Where session files go when neither a repo nor a state dir can be
    resolved. Never the home directory — and, for writers, never silent: a
    session whose identity lands in the temp dir is a misconfiguration worth
    one stderr line. Read-only callers pass ``warn=False``."""
    global _warned_fallback
    root = Path(tempfile.gettempdir()) / "apiary-session-tmp"
    if warn and not _warned_fallback:
        _warned_fallback = True
        print(
            "[apiary] no bootstrapped repo / state dir in scope; session files "
            f"go to {root} (set APIARY_TARGET_REPO / APIARY_TARGET_STATE_DIR "
            "or run inside a bootstrapped repo)",
            file=sys.stderr,
        )
    return root


def session_tmp_dir(warn: bool = True) -> Path:
    """``<repo>/.claude/apiary/session-tmp`` for the current repo.

    Only a *bootstrapped* repo (one with a self-pointer pin) qualifies: a hook
    fired in a plain git checkout must not grow an un-ignored ``.claude/``
    tree there."""
    from core.flags import _per_repo_root
    from core.utils import state
    repo = _per_repo_root()
    if repo is not None and state.read_self_pointer(Path(repo)) is not None:
        return Path(repo) / ".claude" / "apiary" / SESSION_TMP_DIRNAME
    return _fallback_root(warn)


def sessions_dir(warn: bool = True) -> Path:
    """``<state-dir>/sessions`` for the current target.

    ``legacy_in_repo=False``: unlike scribe/compass/researcher/captures,
    session files never fall back to ``<repo>/.apiary/`` — a hook firing in
    a repo apiary does not manage must not grow an un-ignored state tree
    there. The OS temp dir is the fallback instead (see ``_fallback_root``).
    """
    from core.utils.state import resolve_state_dir, state_dir_from_env
    sd = state_dir_from_env()
    if sd is not None:
        return sd / SESSIONS_DIRNAME
    # No launcher env: find the repo the way flags does (CLAUDE_PROJECT_DIR /
    # APIARY_TARGET_REPO, then git), then let the shared resolver read its pins.
    from core.flags import _per_repo_root
    repo = _per_repo_root()
    sd = None if repo is None else resolve_state_dir(
        repo=repo, subdir=SESSIONS_DIRNAME, use_env=False, legacy_in_repo=False,
    )
    return sd if sd is not None else _fallback_root(warn) / SESSIONS_DIRNAME


# Session files are small but numerous (~5 flags + 1 identity per session);
# nothing else deletes them, so the Stop hook sweeps old ones once a day.
FLAG_MAX_AGE_DAYS = 7
IDENTITY_MAX_AGE_DAYS = 30
_SWEEP_STAMP = ".last_sweep"


def sweep_stale_session_files(now: float | None = None, force: bool = False) -> int:
    """Delete hook flags older than ``FLAG_MAX_AGE_DAYS`` and identity files
    older than ``IDENTITY_MAX_AGE_DAYS``; at most once per day unless *force*.
    Returns the number of files removed. Never raises."""
    import time
    now = time.time() if now is None else now
    removed = 0
    try:
        sessions = sessions_dir(warn=False)
        stamp = sessions / _SWEEP_STAMP
        if not force and stamp.exists() and now - stamp.stat().st_mtime < 86400:
            return 0
        targets = [
            (session_tmp_dir(warn=False), "*_*", FLAG_MAX_AGE_DAYS * 86400),
            (sessions, "identity-*.json", IDENTITY_MAX_AGE_DAYS * 86400),
        ]
        for folder, glob, max_age in targets:
            if not folder.is_dir():
                continue
            for f in folder.glob(glob):
                try:
                    if f.is_file() and now - f.stat().st_mtime > max_age:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
        sessions.mkdir(parents=True, exist_ok=True)
        stamp.write_text("", encoding="utf-8")
        import os
        os.utime(stamp, (now, now))
    except OSError:
        pass
    return removed

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_PREFIX_RE = re.compile(r"^[0-9a-f]{8}$", re.IGNORECASE)


class SessionId:
    """Validated session identifier with format helpers.

    Accepts a full UUID (36 chars) or an 8-char hex prefix.
    Provides .full and .short properties and common path builders
    so callers never need to slice or format session IDs manually.
    """

    __slots__ = ("_full", "_short")

    def __init__(self, raw: str):
        raw = raw.strip()
        if not raw:
            raise ValueError("Empty session ID")
        if _UUID_RE.match(raw):
            self._full = raw
            self._short = raw[:8]
        elif _PREFIX_RE.match(raw):
            self._full = None
            self._short = raw.lower()
        else:
            raise ValueError(
                f"Session ID must be a 36-char UUID or 8-char hex prefix, got: {raw!r}"
            )

    # --- properties ---

    @property
    def full(self) -> str:
        """Full UUID. Raises if only the prefix is known."""
        if self._full is None:
            raise ValueError("Full UUID not available — only 8-char prefix was provided")
        return self._full

    @property
    def short(self) -> str:
        """8-char hex prefix."""
        return self._short

    # --- comparison / hashing ---

    def matches(self, other_id: str) -> bool:
        """True if *other_id* starts with this session's prefix (case-insensitive)."""
        return other_id.lower().startswith(self._short)

    def __eq__(self, other):
        if isinstance(other, SessionId):
            return self._short == other._short
        return NotImplemented

    def __hash__(self):
        return hash(self._short)

    def __str__(self):
        return self._full or self._short

    def __repr__(self):
        return f"SessionId({str(self)!r})"

    # --- path helpers ---

    def identity_path(self, base: Path | None = None) -> Path:
        """``<state-dir>/sessions/identity-<short>.json`` (or under *base*)."""
        root = Path(base) if base is not None else sessions_dir()
        return root / f"identity-{self._short}.json"

    def flag_path(self, suffix: str, base: Path | None = None) -> Path:
        """``<repo>/.claude/apiary/session-tmp/<uuid>_<suffix>`` (or under *base*)."""
        root = Path(base) if base is not None else session_tmp_dir()
        return root / f"{self.full}_{suffix}"

    def tmp_path(self, suffix: str, tmp_dir: Path) -> Path:
        """Generic temp file: <tmp_dir>/<full_uuid>_<suffix>"""
        return tmp_dir / f"{self.full}_{suffix}"


def load_identity(session_id=None):
    """Load session identity. If session_id given, read that specific file.
    Otherwise, read the most recently modified identity file.

    *session_id* may be a raw string or a SessionId instance.

    Returns dict with keys: role, mission, registered, session_id,
    wants_role, wants_mission, compass_arm. Defaults if file not found.

    ``compass_arm`` is ``None`` for sessions written before the compass A/B
    existed — callers must treat "not recorded" as distinct from "off"
    (``compass.ab.arm_for_session`` does).
    """
    defaults = {
        "role": "user",
        "mission": "general",
        "registered": True,
        "session_id": "",
        "wants_role": "user",
        "wants_mission": "general",
        "compass_arm": None,
    }

    if session_id:
        sid = session_id if isinstance(session_id, SessionId) else SessionId(session_id)
        identity_file = sid.identity_path(sessions_dir(warn=False))
        if not identity_file.exists():
            return defaults
        files = [identity_file]
    else:
        root = sessions_dir(warn=False)
        try:
            files = sorted(
                root.glob("identity-*.json"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            files = []
        if not files:
            return defaults

    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
        # Extract session_id from filename: identity-<sid>.json
        sid = files[0].stem[len("identity-"):]
        return {
            "role": data.get("role", "user"),
            "mission": data.get("mission", "general"),
            "registered": data.get("registered", True),
            "session_id": sid,
            "wants_role": data.get("wants_role", "user"),
            "wants_mission": data.get("wants_mission", "general"),
            "compass_arm": data.get("compass_arm"),
        }
    except (json.JSONDecodeError, OSError):
        return defaults
