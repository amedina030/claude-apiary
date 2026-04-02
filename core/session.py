"""
Session identity helpers — shared by hooks, scribe, and startup.

Identity files live at ~/.claude/.session-identity-<session_id>.json
and contain: {role, mission, registered, wants_role, wants_mission}.
"""
import json
import re
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"

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

    def identity_path(self, claude_dir: Path = CLAUDE_DIR) -> Path:
        return claude_dir / f".session-identity-{self._short}.json"

    def flag_path(self, suffix: str, claude_dir: Path = CLAUDE_DIR) -> Path:
        return claude_dir / "tmp" / f"{self.full}_{suffix}"

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
        files = sorted(
            CLAUDE_DIR.glob(".session-identity-*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return defaults

    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
        # Extract session_id from filename: .session-identity-<sid>.json
        sid = files[0].stem.replace(".session-identity-", "")
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
