"""apiary observer — PostToolUse adapter layer for durable tool-call records.

Observer scripts under ``<apiary-repo>/observers/*.py`` use this module to
read Claude Code PostToolUse payloads as a stable :class:`ObservationEvent`
and dispatch their own handler while the harness catches every failure and
guarantees exit 0 so the originating tool call is never blocked.

Public API::

    from observer import run_observer, ObservationEvent
    from observer import AdapterError, SCHEMA_VERSION, from_payload, from_stdin
"""
from .adapter import (
    SCHEMA_VERSION,
    AdapterError,
    ObservationEvent,
    RawPayload,
    from_payload,
    from_stdin,
)
from .failure import run_observer

__all__ = [
    "SCHEMA_VERSION",
    "AdapterError",
    "ObservationEvent",
    "RawPayload",
    "from_payload",
    "from_stdin",
    "run_observer",
]
