"""
Flag file management for claude-apiary tools.

Each tool feature is toggled via a sentinel file at ~/.claude/{flag_name}-enabled.
"""
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"


def is_enabled(flag_name: str) -> bool:
    """Return True if ~/.claude/{flag_name}-enabled exists."""
    return (CLAUDE_DIR / f"{flag_name}-enabled").exists()


def enable(flag_name: str) -> None:
    """Create ~/.claude/{flag_name}-enabled."""
    flag_file = CLAUDE_DIR / f"{flag_name}-enabled"
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    flag_file.write_text("enabled", encoding="utf-8")


def disable(flag_name: str) -> None:
    """Delete ~/.claude/{flag_name}-enabled if it exists."""
    flag_file = CLAUDE_DIR / f"{flag_name}-enabled"
    if flag_file.exists():
        flag_file.unlink()


def toggle(flag_name: str) -> bool:
    """Toggle flag. Returns new state (True = enabled)."""
    if is_enabled(flag_name):
        disable(flag_name)
        return False
    enable(flag_name)
    return True
