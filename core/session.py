"""
Session identity helpers — shared by hooks, scribe, and startup.

Identity files live at ~/.claude/.session-identity-<session_id>.json
and contain: {role, mission, registered, wants_role, wants_mission}.
"""
import json
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"


def load_identity(session_id=None):
    """Load session identity. If session_id given, read that specific file.
    Otherwise, read the most recently modified identity file.

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
        identity_file = CLAUDE_DIR / f".session-identity-{session_id[:8]}.json"
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
