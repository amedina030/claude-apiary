"""Cron-job health check and repair tool.

Detects drift between apiary's canonical scheduled-entry registry
(``cron_registry/<hostname>.json`` at the repo root — one file per host, so
two machines scheduling the same repo don't overwrite each other) and what
the host's OS scheduler has actually registered, and optionally repairs it.

Trigger case this addresses: the ``pipeline/`` → ``runner/`` rename
silently broke the registered overnight cron because it still pointed
at ``pipeline/run.py``. This tool surfaces that class of drift and
gives the operator a single command to fix it.

Scope (this release):
  - Windows Task Scheduler backend only; the ``SchedulerBackend``
    protocol (runner/schedulers/base.py) keeps launchd and crontab
    cheap to add later.
  - Subcommands: ``check`` (read-only) and ``repair``
    (dry-run by default, ``--apply`` to execute).

Invoke via the apiary launcher:

    python -m runner.cron_health check          # from the apiary checkout
    python -m runner.cron_health repair --apply  # from the apiary checkout

Exit codes:
  0 — healthy, or dry-run produced no errors
  1 — drift detected (check) or apply failed on one or more entries
  2 — config or platform error (no registry, malformed JSON,
      unsupported OS, scheduler binary not on PATH)
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .schedulers.base import (
    ObservedEntry, SchedulerBackend, SchedulerError, UnsupportedPlatformError,
)
from core.hooks_lib import resolve_python

SCRIPT_DIR = Path(__file__).resolve().parent
APIARY_REPO_ROOT = SCRIPT_DIR.parent
CRON_REGISTRY_DIR = APIARY_REPO_ROOT / "cron_registry"


def _hostname() -> str:
    """Sanitized hostname for use as a filename. Falls back to 'unknown'."""
    raw = platform.node().strip()
    if not raw:
        return "unknown"
    # Strip characters that can be ugly or illegal in paths on any OS.
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in raw)
    return safe or "unknown"


def registry_path_for_host(hostname: Optional[str] = None) -> Path:
    """Return the per-host registry path. Defaults to the current hostname."""
    return CRON_REGISTRY_DIR / f"{hostname or _hostname()}.json"


# Kept at module scope so tests can still ``mock.patch.object(cron_health,
# "REGISTRY_PATH", tempfile)``. Recomputed per-hostname at load time when not
# explicitly overridden.
REGISTRY_PATH = registry_path_for_host()
PREFIX = "\\apiary\\"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_CONFIG_ERROR = 2


@dataclass(frozen=True)
class RegistryEntry:
    """A canonical registry entry with placeholders resolved."""
    entry_id: str
    description: str
    command: list[str]
    cwd: str
    schedule: dict
    disabled: bool = False

    @classmethod
    def from_raw(cls, raw: dict, apiary_root: Path) -> "RegistryEntry":
        entry_id = raw["id"]
        return cls(
            entry_id=entry_id,
            description=raw.get("description", ""),
            command=[_resolve_placeholders(tok, apiary_root) for tok in raw["command"]],
            cwd=_resolve_placeholders(raw.get("cwd", ""), apiary_root),
            schedule=dict(raw.get("schedule", {})),
            disabled=bool(raw.get("disabled", False)),
        )


@dataclass
class EntryStatus:
    """Classification result for a single registry entry."""
    entry: RegistryEntry
    state: str  # "ok", "missing", or "broken"
    reason: str = ""
    observed: Optional[ObservedEntry] = None


class RegistryError(RuntimeError):
    """Raised when the registry file is missing or malformed."""


def _resolve_placeholders(value: str, apiary_root: Path) -> str:
    """Replace registry placeholders with live, machine-specific values.

    - ``<apiary_repo>`` → the live apiary root path.
    - ``<python>``      → the resolved Python 3 interpreter (honors the
      ``APIARY_PYTHON`` override; see ``core/hooks_lib.resolve_python``).

    The ``<python>`` token keeps a registry portable: a scheduled task fires
    in a fresh process with no inherited interpreter, and no bare name is
    present on every OS (``python`` is absent on macOS Homebrew, ``python3``
    on stock Windows). ``<python>`` resolves to whatever this machine has —
    a native absolute path, since the OS scheduler runs the action directly,
    not through a shell. A literal token (the legacy ``"python"``) is left
    untouched, so existing registries keep working unchanged.
    """
    if not value:
        return ""
    value = value.replace("<apiary_repo>", str(apiary_root))
    value = value.replace("<python>", str(resolve_python()))
    return value


def load_registry(
    path: Path = REGISTRY_PATH, apiary_root: Path = APIARY_REPO_ROOT,
) -> list[RegistryEntry]:
    """Load and validate the registry. Raises RegistryError on problems."""
    if not path.exists():
        raise RegistryError(
            f"registry not found at {path}; create it to start managing "
            f"scheduled tasks"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistryError(
            f"registry at {path} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise RegistryError(
            f"registry at {path}: 'entries' must be a list"
        )
    out: list[RegistryEntry] = []
    seen: set[str] = set()
    for i, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise RegistryError(
                f"registry at {path}: entries[{i}] must be an object"
            )
        if "id" not in raw or not isinstance(raw["id"], str) or not raw["id"]:
            raise RegistryError(
                f"registry at {path}: entries[{i}] missing 'id' (non-empty string)"
            )
        if raw["id"] in seen:
            raise RegistryError(
                f"registry at {path}: duplicate entry id '{raw['id']}'"
            )
        seen.add(raw["id"])
        if "command" not in raw or not isinstance(raw["command"], list) or not raw["command"]:
            raise RegistryError(
                f"registry at {path}: entries[{i}] ('{raw['id']}') must have "
                f"a non-empty 'command' list"
            )
        out.append(RegistryEntry.from_raw(raw, apiary_root))
    return out


def get_scheduler() -> SchedulerBackend:
    """Return the backend for the current platform.

    Windows → ``WindowsTaskScheduler``. Other platforms →
    ``UnsupportedPlatformError``.
    """
    if sys.platform != "win32":
        raise UnsupportedPlatformError(
            f"no scheduler backend for {sys.platform}; "
            f"Windows Task Scheduler is the only backend implemented in "
            f"this release"
        )
    if shutil.which("schtasks") is None:
        raise UnsupportedPlatformError(
            "scheduler backend unavailable (schtasks not found on PATH; "
            "this tool currently supports Windows only)"
        )
    # Lazy import so non-Windows systems don't pay the import cost and
    # any Windows-specific imports in the module (future) stay quarantined.
    from .schedulers.windows import WindowsTaskScheduler
    return WindowsTaskScheduler()


def classify_entry(
    entry: RegistryEntry, observed: Optional[ObservedEntry],
) -> EntryStatus:
    """Decide whether a registry entry is ok/broken/missing vs what's observed.

    Drift checks:
      - ``disabled: true`` entry that's present in the scheduler → broken
      - registry entry with no matching scheduler entry → missing
      - command drift (rendered form differs)
      - script path missing (for literal-script commands)
      - cwd missing on disk
      - schedule drift
    """
    if entry.disabled:
        if observed is None:
            return EntryStatus(entry=entry, state="ok")
        return EntryStatus(
            entry=entry, state="broken", observed=observed,
            reason="should not exist (disabled: true)",
        )
    if observed is None:
        return EntryStatus(entry=entry, state="missing")
    if entry.cwd and not Path(entry.cwd).is_dir():
        return EntryStatus(
            entry=entry, state="broken", observed=observed,
            reason="cwd missing",
        )
    script = _literal_script_path(entry.command)
    if script is not None and not Path(script).exists():
        return EntryStatus(
            entry=entry, state="broken", observed=observed,
            reason="script path missing",
        )
    if _normalize_command(entry.command) != _normalize_command(observed.command):
        return EntryStatus(
            entry=entry, state="broken", observed=observed,
            reason="command drift",
        )
    if _normalize_schedule(entry.schedule) != _normalize_schedule(observed.schedule):
        return EntryStatus(
            entry=entry, state="broken", observed=observed,
            reason="schedule drift",
        )
    return EntryStatus(entry=entry, state="ok", observed=observed)


def _literal_script_path(command: list[str]) -> Optional[str]:
    """If ``command`` invokes a literal ``.py`` file, return that path.

    Module invocations (``-m <module>``) are NOT probed — the source-tree
    check would be brittle and is explicitly out of scope. Those entries
    rely on the ``cwd`` check instead.
    """
    if len(command) < 2:
        return None
    first = Path(command[0]).name.lower()
    if not first.startswith("python"):
        return None
    for i in range(1, len(command)):
        token = command[i]
        if token == "-m":
            return None  # module invocation, not a script file
        if token.endswith(".py"):
            return token
        if token.startswith("-"):
            continue  # a flag, not the script path yet
    return None


def _normalize_command(command: Iterable[str]) -> list[str]:
    """Lowercase-compare tokens, with Windows path separators unified."""
    return [c.replace("\\", "/").lower() for c in command]


def _normalize_schedule(schedule: dict) -> dict:
    """Lowercase ``type`` so ``'Daily'`` and ``'daily'`` compare equal."""
    return {
        "type": str(schedule.get("type", "")).lower(),
        "time": str(schedule.get("time", "")).strip(),
    }


def diff(
    registry: list[RegistryEntry], observed: list[ObservedEntry],
) -> list[EntryStatus]:
    """Classify each registry entry against the observed state."""
    by_id = {e.entry_id: e for e in observed}
    return [classify_entry(e, by_id.get(e.entry_id)) for e in registry]


def format_table(statuses: list[EntryStatus]) -> str:
    """Render a human-readable table of per-entry status."""
    if not statuses:
        return "(no registry entries)\n"
    rows = [("ID", "STATE", "DETAIL")]
    for s in statuses:
        if s.state == "ok":
            detail = s.entry.description or ""
        elif s.state == "missing":
            detail = "not present in scheduler"
        else:
            detail = s.reason or "broken"
        rows.append((s.entry.entry_id, s.state, detail))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    lines = []
    for i, row in enumerate(rows):
        line = "  ".join(row[j].ljust(widths[j]) for j in range(3)).rstrip()
        lines.append(line)
        if i == 0:
            lines.append("  ".join("-" * widths[j] for j in range(3)))
    return "\n".join(lines) + "\n"


def check(
    registry: list[RegistryEntry], backend: SchedulerBackend,
) -> tuple[list[EntryStatus], int]:
    """Read-only inspection. Returns (statuses, exit_code)."""
    observed = backend.list_entries(PREFIX)
    statuses = diff(registry, observed)
    exit_code = EXIT_OK if all(s.state == "ok" for s in statuses) else EXIT_DRIFT
    return statuses, exit_code


def repair(
    registry: list[RegistryEntry], backend: SchedulerBackend, *, apply: bool,
) -> tuple[list[EntryStatus], list[str], int]:
    """Fix drift. Returns (post_statuses, action_log, exit_code).

    With ``apply=False`` the scheduler is not modified; action_log
    describes what *would* be done and the original statuses are returned
    as the post state. With ``apply=True`` broken entries are deleted and
    (unless ``disabled``) recreated, then the scheduler is re-queried so
    the returned statuses reflect the post-state.
    """
    pre_statuses, _ = check(registry, backend)
    needs_action = [s for s in pre_statuses if s.state != "ok"]
    actions: list[str] = []
    if not apply:
        for s in needs_action:
            actions.append(_plan_line(s))
        return pre_statuses, actions, EXIT_OK
    any_failed = False
    for s in needs_action:
        try:
            if s.observed is not None:
                backend.delete_entry(PREFIX, s.entry.entry_id)
                actions.append(f"deleted {s.entry.entry_id}")
            if not s.entry.disabled:
                backend.create_entry(
                    PREFIX, s.entry.entry_id, s.entry.command,
                    s.entry.cwd, s.entry.schedule,
                )
                actions.append(f"created {s.entry.entry_id}")
        except SchedulerError as exc:
            any_failed = True
            actions.append(
                f"FAILED {s.entry.entry_id}: {exc}"
                + (f"\n  stderr: {exc.stderr.strip()}" if exc.stderr else "")
            )
    post_statuses, post_exit = check(registry, backend)
    if any_failed:
        return post_statuses, actions, EXIT_DRIFT
    return post_statuses, actions, post_exit


def _plan_line(status: EntryStatus) -> str:
    if status.state == "missing":
        return f"would create {status.entry.entry_id}"
    if status.entry.disabled:
        return f"would delete {status.entry.entry_id} (disabled)"
    return f"would delete and recreate {status.entry.entry_id} ({status.reason})"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cron_health",
        description=(
            "Check or repair the host scheduler's entries against "
            "apiary's canonical registry."
        ),
    )
    subs = parser.add_subparsers(dest="cmd", required=True)
    subs.add_parser("check", help="read-only inspection")
    repair_p = subs.add_parser(
        "repair",
        help="dry-run by default; pass --apply to execute changes",
    )
    repair_p.add_argument(
        "--apply", action="store_true",
        help="execute the planned changes (default: dry-run)",
    )
    args = parser.parse_args(argv)

    try:
        registry = load_registry(REGISTRY_PATH, APIARY_REPO_ROOT)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        backend = get_scheduler()
    except UnsupportedPlatformError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        if args.cmd == "check":
            statuses, rc = check(registry, backend)
            sys.stdout.write(format_table(statuses))
            return rc
        # repair
        statuses, actions, rc = repair(registry, backend, apply=args.apply)
        sys.stdout.write(format_table(statuses))
        if actions:
            sys.stdout.write("\n")
            for line in actions:
                sys.stdout.write(f"  {line}\n")
        return rc
    except SchedulerError as exc:
        print(f"scheduler error: {exc}", file=sys.stderr)
        if exc.stderr:
            print(f"stderr: {exc.stderr.strip()}", file=sys.stderr)
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main())
