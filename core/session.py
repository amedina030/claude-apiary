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


def _fallback_root() -> Path:
    """Where session files go when neither a repo nor a state dir can be
    resolved. Never the home directory — and never silent: a session whose
    identity lands in the temp dir is a misconfiguration worth one stderr line."""
    global _warned_fallback
    root = Path(tempfile.gettempdir()) / "apiary-session-tmp"
    if not _warned_fallback:
        _warned_fallback = True
        print(
            "[apiary] no bootstrapped repo / state dir in scope; session files "
            f"go to {root} (set APIARY_TARGET_REPO / APIARY_TARGET_STATE_DIR "
            "or run inside a bootstrapped repo)",
            file=sys.stderr,
        )
    return root


def session_tmp_dir() -> Path:
    """``<repo>/.claude/apiary/session-tmp`` for the current repo."""
    from core.flags import _per_repo_root
    repo = _per_repo_root()
    if repo is not None:
        return Path(repo) / ".claude" / "apiary" / SESSION_TMP_DIRNAME
    return _fallback_root()


def sessions_dir() -> Path:
    """``<state-dir>/sessions`` for the current target."""
    from core.utils import state
    sd = state.state_dir_from_env()
    if sd is None:
        from core.flags import _per_repo_root
        repo = _per_repo_root()
        if repo is not None:
            sd = state.find_state_dir(Path(repo))
    if sd is None:
        return _fallback_root() / SESSIONS_DIRNAME
    return Path(sd) / SESSIONS_DIRNAME

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
    wants_role, wants_mission. Defaults if file not found.
    """
    defaults = {
        "role": "user",
        "mission": "general",
        "registered": True,
        "session_id": "",
        "wants_role": "user",
        "wants_mission": "general",
    }

    if session_id:
        sid = session_id if isinstance(session_id, SessionId) else SessionId(session_id)
        identity_file = sid.identity_path()
        if not identity_file.exists():
            return defaults
        files = [identity_file]
    else:
        root = sessions_dir()
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
        }
    except (json.JSONDecodeError, OSError):
        return defaults
