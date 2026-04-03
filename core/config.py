"""
Config loading utilities for claude-apiary tools.
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional


def load_config(path: Path, defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load JSON config from path. Missing keys fall back to defaults."""
    base = defaults.copy() if defaults else {}
    if not path.exists():
        return base
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {**base, **data}


def write_config(path: Path, config: Dict[str, Any]) -> None:
    """Write config as formatted JSON, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
