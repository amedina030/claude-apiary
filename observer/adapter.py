"""Observer adapter — reads Claude Code PostToolUse payloads into a stable event.

The adapter quarantines per-tool variance (``tool_input``/``tool_response``
shapes differ by tool) under :attr:`ObservationEvent.raw` and exposes only
framework-level common fields as top-level attributes. Bumping
:data:`SCHEMA_VERSION` is how we signal that observer scripts need updating
when the Claude Code payload contract changes.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = 1

_MAX_STDIN_BYTES = 64 * 1024


class AdapterError(Exception):
    """Raised when stdin is empty, malformed, or missing required fields."""


@dataclass
class RawPayload:
    """Per-tool raw fields quarantined from the stable event contract."""

    tool_input: Optional[dict] = None
    tool_response: Optional[Any] = None


@dataclass
class ObservationEvent:
    """Stable cross-tool PostToolUse event shape."""

    schema_version: int
    session_id: str
    hook_event_name: str
    tool_name: str
    tool_use_id: str
    transcript_path: str
    cwd: str
    permission_mode: str
    raw: RawPayload = field(default_factory=RawPayload)


def from_payload(payload: Any) -> ObservationEvent:
    """Build an :class:`ObservationEvent` from an already-parsed JSON payload.

    Raises :class:`AdapterError` if the payload is not a dict or is missing
    required framework-level fields. Per-tool fields (``tool_input``,
    ``tool_response``) are preserved verbatim under :attr:`ObservationEvent.raw`
    and may be ``None`` without raising.
    """
    if not isinstance(payload, dict):
        raise AdapterError(
            f"payload is not a JSON object: got {type(payload).__name__}"
        )

    hook_event_name = payload.get("hook_event_name")
    if not hook_event_name or not isinstance(hook_event_name, str):
        raise AdapterError("payload missing required field 'hook_event_name'")

    tool_name = payload.get("tool_name")
    if not tool_name or not isinstance(tool_name, str):
        raise AdapterError("payload missing required field 'tool_name'")

    return ObservationEvent(
        schema_version=SCHEMA_VERSION,
        session_id=_str_field(payload, "session_id"),
        hook_event_name=hook_event_name,
        tool_name=tool_name,
        tool_use_id=_str_field(payload, "tool_use_id"),
        transcript_path=_str_field(payload, "transcript_path"),
        cwd=_str_field(payload, "cwd"),
        permission_mode=_str_field(payload, "permission_mode"),
        raw=RawPayload(
            tool_input=payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else None,
            tool_response=payload.get("tool_response"),
        ),
    )


def from_stdin() -> ObservationEvent:
    """Read a PostToolUse payload from stdin and return an :class:`ObservationEvent`.

    Reads at most :data:`_MAX_STDIN_BYTES` bytes. Raises :class:`AdapterError`
    on empty stdin, truncated input, malformed JSON, or missing required fields.
    Observer scripts should prefer :func:`observer.run_observer` which wraps
    this call in the full failure-path contract.
    """
    raw = _read_stdin_bytes()
    payload = _parse_json_bytes(raw)
    return from_payload(payload)


def _read_stdin_bytes() -> bytes:
    try:
        data = sys.stdin.buffer.read(_MAX_STDIN_BYTES)
    except OSError as exc:
        raise AdapterError(f"stdin read failed: {exc}") from exc
    if not data:
        raise AdapterError("stdin is empty")
    return data


def _parse_json_bytes(data: bytes) -> Any:
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"stdin is not valid JSON: {exc}") from exc


def _str_field(payload: dict, key: str) -> str:
    value = payload.get(key, "")
    return value if isinstance(value, str) else ""
