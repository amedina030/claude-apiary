"""Shared config loader for the runner."""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

_config = None


def load_config() -> dict:
    """Load runner config. Returns empty dict if config file missing."""
    global _config
    if _config is not None:
        return _config
    if CONFIG_PATH.exists():
        _config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        _config = {}
    return _config


def get(section: str, key: str, default=None):
    """Get a config value: get("harden", "max_rounds", 3)."""
    return load_config().get(section, {}).get(key, default)
