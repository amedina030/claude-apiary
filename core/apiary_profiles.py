"""Apiary profile loader — resolves extends chain + deep-merges JSONC profiles.

A profile is ``<apiary-repo>/profiles/<name>.jsonc`` with fields:

- ``$schema_version`` (int, required) — must be 1 for this loader
- ``extends`` (list of profile names, optional) — parents merged left-to-right
- any other keys — carried through verbatim and merged into the resolved view

Merge rules: dicts recurse, lists concatenate, scalars last-write-wins.
A value wrapped as ``{"$replace": <value>}`` replaces rather than merges.
Cycles in the extends graph raise :class:`ProfileCycleError`. Unknown
``$schema_version`` raises :class:`ProfileSchemaError`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.utils.jsonc import JsoncParseError
from core.utils.jsonc import load as load_jsonc

SUPPORTED_SCHEMA_VERSION = 1
_REPLACE_MARKER = "$replace"


class ProfileError(Exception):
    """Base class for profile-resolution errors."""


class ProfileNotFoundError(ProfileError):
    """Raised when a profile name doesn't exist in the profiles directory."""

    def __init__(self, name: str, profiles_dir: Path, available: list[str]):
        self.name = name
        self.profiles_dir = profiles_dir
        self.available = available
        listing = ", ".join(available) if available else "(none)"
        super().__init__(f"profile '{name}' not found in {profiles_dir}. Available: {listing}")


class ProfileCycleError(ProfileError):
    """Raised when the extends graph contains a cycle."""

    def __init__(self, cycle_path: list[str]):
        self.cycle_path = cycle_path
        super().__init__("extends cycle: " + " -> ".join(cycle_path))


class ProfileSchemaError(ProfileError):
    """Raised when a profile's $schema_version is missing or unsupported."""


class ProfileMergeError(ProfileError):
    """Raised when a merge encounters a malformed $replace wrapper."""


@dataclass
class ResolvedProfile:
    """Output of :func:`resolve` — the merged profile plus provenance."""

    name: str
    profiles_applied: list[str]
    merged: dict
    content_hashes: dict[str, str] = field(default_factory=dict)


def resolve(name: str, profiles_dir: Path) -> ResolvedProfile:
    """Load ``name`` from ``profiles_dir``, walk extends, return merged view.

    Applies parents in ``extends`` order (left-to-right), then the child on
    top. ``profiles_applied`` lists all profiles in merge order, parents
    before children. Re-encountering a profile under the same root is a
    cycle and raises :class:`ProfileCycleError`.
    """
    applied_order: list[str] = []
    hashes: dict[str, str] = {}
    merged = _resolve_recursive(
        name=name,
        profiles_dir=profiles_dir,
        stack=[],
        applied_order=applied_order,
        hashes=hashes,
    )
    return ResolvedProfile(
        name=name,
        profiles_applied=applied_order,
        merged=merged,
        content_hashes=hashes,
    )


def list_available(profiles_dir: Path) -> list[str]:
    """Return profile names found under ``profiles_dir`` (``*.jsonc`` stems)."""
    if not profiles_dir.is_dir():
        return []
    return sorted(p.stem for p in profiles_dir.glob("*.jsonc"))


def _resolve_recursive(
    *,
    name: str,
    profiles_dir: Path,
    stack: list[str],
    applied_order: list[str],
    hashes: dict[str, str],
) -> dict:
    if name in stack:
        raise ProfileCycleError(stack + [name])
    path = profiles_dir / f"{name}.jsonc"
    if not path.is_file():
        raise ProfileNotFoundError(name, profiles_dir, list_available(profiles_dir))
    try:
        raw = load_jsonc(path)
    except JsoncParseError:
        raise
    if not isinstance(raw, dict):
        raise ProfileSchemaError(f"{path}: profile must be a JSON object")
    _validate_schema_version(path, raw)
    hashes[name] = _hash_file(path)
    extends = raw.get("extends", []) or []
    if not isinstance(extends, list):
        raise ProfileSchemaError(f"{path}: 'extends' must be a list of profile names")

    accumulated: dict = {}
    for parent_name in extends:
        if not isinstance(parent_name, str):
            raise ProfileSchemaError(
                f"{path}: 'extends' entries must be strings, got {type(parent_name).__name__}"
            )
        parent_merged = _resolve_recursive(
            name=parent_name,
            profiles_dir=profiles_dir,
            stack=stack + [name],
            applied_order=applied_order,
            hashes=hashes,
        )
        accumulated = deep_merge(accumulated, parent_merged)

    own = {k: v for k, v in raw.items() if k not in ("$schema_version", "extends")}
    merged = deep_merge(accumulated, own)
    applied_order.append(name)
    return merged


def _validate_schema_version(path: Path, raw: dict) -> None:
    version = raw.get("$schema_version")
    if version is None:
        raise ProfileSchemaError(f"{path}: missing required '$schema_version'")
    if not isinstance(version, int):
        raise ProfileSchemaError(
            f"{path}: '$schema_version' must be an integer, got {type(version).__name__}"
        )
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ProfileSchemaError(
            f"{path}: $schema_version={version} not supported by this apiary "
            f"(expected {SUPPORTED_SCHEMA_VERSION}). Upgrade apiary."
        )


def _hash_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def deep_merge(base: Any, overlay: Any) -> Any:
    """Merge ``overlay`` into ``base``. Non-mutating — returns a new value.

    - If overlay is a ``$replace`` wrapper, the wrapped value replaces base.
    - Two dicts → recursive per-key merge.
    - Two lists → concatenation (base + overlay).
    - Anything else → overlay wins.
    """
    if _is_replace_wrapper(overlay):
        return _strip_replace_wrappers(_unwrap_replace(overlay))
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for key, ov_val in overlay.items():
            if _is_replace_wrapper(ov_val):
                out[key] = _strip_replace_wrappers(_unwrap_replace(ov_val))
            elif key in out:
                out[key] = deep_merge(out[key], ov_val)
            else:
                out[key] = _strip_replace_wrappers(ov_val)
        return out
    if isinstance(base, list) and isinstance(overlay, list):
        return list(base) + list(overlay)
    return _strip_replace_wrappers(overlay)


def _strip_replace_wrappers(value: Any) -> Any:
    """Recursively unwrap ``$replace`` wrappers so they don't leak into output."""
    if _is_replace_wrapper(value):
        return _strip_replace_wrappers(_unwrap_replace(value))
    if isinstance(value, dict):
        return {k: _strip_replace_wrappers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_replace_wrappers(v) for v in value]
    return value


def _is_replace_wrapper(value: Any) -> bool:
    return isinstance(value, dict) and _REPLACE_MARKER in value


def _unwrap_replace(value: Any) -> Any:
    if not _is_replace_wrapper(value):
        return value
    if len(value) != 1:
        raise ProfileMergeError(
            f"'{_REPLACE_MARKER}' must be the sole key; extra keys: "
            + ", ".join(k for k in value if k != _REPLACE_MARKER)
        )
    return value[_REPLACE_MARKER]
