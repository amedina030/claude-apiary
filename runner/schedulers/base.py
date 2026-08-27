"""Scheduler backend protocol for cron_health.

Each OS-level scheduler (Windows Task Scheduler, launchd, crontab) implements
``SchedulerBackend``. ``cron_health`` only talks to the backend through this
interface so new backends can be added without touching the orchestrator.

The registry stores scheduled entries in a backend-agnostic shape
(``RegistryEntry`` in cron_health) and each backend is responsible for
translating to and from its native representation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass(frozen=True)
class ObservedEntry:
    """How a backend reports a scheduled entry it currently has.

    The backend canonicalizes its native fields into this shape so
    cron_health can compare observed state to the registry without
    knowing Windows- or Unix-specific details.

    - ``entry_id`` is the apiary-side handle (the portion after the
      ``\\apiary\\`` prefix on Windows, or equivalent elsewhere).
    - ``command`` is the list-form command; backends parse their
      native single-string representation into a list.
    - ``cwd`` is the working directory the scheduler will launch from.
    - ``schedule`` is the backend's canonical schedule dict
      (``{"type": "daily", "time": "02:00"}`` etc.) matching the
      registry's schedule shape.
    """
    entry_id: str
    command: list[str]
    cwd: str
    schedule: dict = field(default_factory=dict)


class SchedulerBackend(Protocol):
    """Minimum surface a backend must expose to cron_health."""

    name: str

    def list_entries(self, prefix: str) -> list[ObservedEntry]:
        """Return every scheduled entry whose native name sits under ``prefix``.

        Entries outside the prefix must never appear in the result so
        cron_health's 'don't touch foreign entries' invariant holds
        at the backend boundary.
        """
        ...

    def create_entry(
        self, prefix: str, entry_id: str, command: list[str],
        cwd: str, schedule: dict,
    ) -> None:
        """Register a new entry under ``<prefix><entry_id>``.

        Raises ``SchedulerError`` on failure with stderr captured.
        """
        ...

    def delete_entry(self, prefix: str, entry_id: str) -> None:
        """Remove the entry named ``<prefix><entry_id>``.

        Raises ``SchedulerError`` on failure with stderr captured.
        """
        ...


class SchedulerError(RuntimeError):
    """Raised when a backend's native tool (schtasks, crontab, etc.) fails."""

    def __init__(self, message: str, stderr: Optional[str] = None):
        super().__init__(message)
        self.stderr = stderr or ""


class UnsupportedPlatformError(RuntimeError):
    """Raised when cron_health is run on a platform with no available backend."""
