"""Windows Task Scheduler backend for cron_health.

Shells out to ``schtasks`` for query, create, and delete. Entries apiary
manages all live under a shared prefix (``\\apiary\\``) so foreign entries
on the user's machine are never touched.

The module imports cleanly on non-Windows hosts. All OS-specific calls
happen inside ``WindowsTaskScheduler`` methods, which cron_health only
instantiates after a platform check.
"""
from __future__ import annotations

import csv
import io
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

from .base import ObservedEntry, SchedulerBackend, SchedulerError


class WindowsTaskScheduler(SchedulerBackend):
    """SchedulerBackend backed by the Windows ``schtasks`` binary."""

    name = "windows"

    def __init__(self, schtasks_bin: str = "schtasks") -> None:
        # Allow tests to inject a fake binary path. Production code uses
        # the default which resolves via PATH.
        self._schtasks = schtasks_bin

    def list_entries(self, prefix: str) -> list[ObservedEntry]:
        """Parse ``schtasks /query /fo CSV /v`` output, filtered to ``prefix``."""
        result = self._run([
            self._schtasks, "/query", "/fo", "CSV", "/v",
        ])
        if result.returncode != 0:
            raise SchedulerError(
                "schtasks /query failed", stderr=result.stderr,
            )
        rows = _parse_schtasks_csv(result.stdout)
        entries: list[ObservedEntry] = []
        for row in rows:
            name = row.get("TaskName", "")
            if not name.startswith(prefix):
                continue
            entry_id = name[len(prefix):]
            if not entry_id:
                # Just the prefix folder itself — skip.
                continue
            command = _parse_task_to_run(row.get("Task To Run", ""))
            # Tasks are registered via /XML with a <WorkingDirectory>, so
            # the observed cwd comes back through the "Start In" column.
            # schtasks reports "N/A" when no working directory is set.
            raw_cwd = (row.get("Start In", "") or "").strip()
            cwd = "" if raw_cwd.upper() == "N/A" else raw_cwd
            schedule = _parse_schedule(row)
            entries.append(ObservedEntry(
                entry_id=entry_id, command=command,
                cwd=cwd, schedule=schedule,
            ))
        return entries

    def create_entry(
        self, prefix: str, entry_id: str, command: list[str],
        cwd: str, schedule: dict,
    ) -> None:
        full_name = prefix + entry_id
        # Build an XML task definition and pass it via /Create /XML.
        # schtasks' /Create CLI flags cannot set <WorkingDirectory>, which
        # apiary tasks need (e.g. `python -m runner.run` requires the
        # repo root on the cwd to resolve the runner package).
        _validate_schedule_for_create(schedule)
        xml_body = _build_task_xml(command, cwd, schedule)
        xml_file = _write_xml_utf16(xml_body)
        try:
            result = self._run([
                self._schtasks, "/Create", "/TN", full_name,
                "/XML", str(xml_file), "/F",
            ])
        finally:
            try:
                xml_file.unlink()
            except OSError:
                pass
        if result.returncode != 0:
            raise SchedulerError(
                f"schtasks /Create failed for {full_name}",
                stderr=result.stderr,
            )

    def delete_entry(self, prefix: str, entry_id: str) -> None:
        full_name = prefix + entry_id
        result = self._run([
            self._schtasks, "/Delete", "/tn", full_name, "/f",
        ])
        if result.returncode != 0:
            raise SchedulerError(
                f"schtasks /Delete failed for {full_name}",
                stderr=result.stderr,
            )

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        # Seam for tests to substitute subprocess behavior.
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
        )


def _parse_schtasks_csv(text: str) -> list[dict]:
    """Parse the multi-header CSV that ``schtasks /query /fo CSV /v`` emits.

    Verbose CSV mode re-prints the header row between every task's block.
    ``csv.DictReader`` would treat those header rows as data rows. We drop
    any row where the ``TaskName`` field equals the literal ``"TaskName"``.
    """
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for row in reader:
        if row.get("TaskName") == "TaskName":
            continue
        rows.append(row)
    return rows


def _parse_task_to_run(task_to_run: str) -> list[str]:
    """Split schtasks' single-string ``Task To Run`` back into a list.

    schtasks reports the action as one string (e.g.
    ``python -m runner.run --detached``). Backslashes in Windows paths
    (``D:\\apiary``) are treated as literal here — ``shlex`` in posix
    mode would consume them as escapes. We use non-posix mode and
    strip any surrounding quotes schtasks adds around quoted tokens.
    """
    if not task_to_run:
        return []
    try:
        # posix=False preserves backslashes as literal path separators.
        return shlex.split(task_to_run, posix=False)
    except ValueError:
        return task_to_run.split()


def _build_task_xml(command: list[str], cwd: str, schedule: dict) -> str:
    """Build a Task Scheduler 2.0 XML document for /Create /XML input.

    Encodes the command as ``<Command>`` + ``<Arguments>``, the cwd as
    ``<WorkingDirectory>``, and the schedule as a daily CalendarTrigger.
    Only ``schedule.type == "daily"`` is supported (see
    ``_validate_schedule_for_create``).
    """
    if not command:
        raise SchedulerError("cannot create a task with an empty command")
    exe = xml_escape(command[0])
    args = xml_escape(" ".join(_xml_arg_quote(a) for a in command[1:]))
    start_time = schedule.get("time") or "00:00"
    start_boundary = f"2026-01-01T{start_time}:00"
    work_dir = xml_escape(cwd) if cwd else ""
    work_dir_xml = (
        f"      <WorkingDirectory>{work_dir}</WorkingDirectory>\n"
        if work_dir else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <Triggers>\n"
        "    <CalendarTrigger>\n"
        f"      <StartBoundary>{start_boundary}</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
        "    </CalendarTrigger>\n"
        "  </Triggers>\n"
        '  <Principals>\n'
        '    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT72H</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{exe}</Command>\n"
        f"      <Arguments>{args}</Arguments>\n"
        f"{work_dir_xml}"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _xml_arg_quote(arg: str) -> str:
    """Quote an argument for embedding in the XML ``<Arguments>`` string."""
    if not arg:
        return '""'
    if any(ch in arg for ch in (" ", "\t", '"')):
        escaped = arg.replace('"', '\\"')
        return f'"{escaped}"'
    return arg


def _write_xml_utf16(body: str) -> Path:
    """Write the XML body to a temp file in UTF-16 LE with BOM.

    schtasks /XML expects UTF-16; feeding it UTF-8 causes silent parse
    failures that only show up when schtasks returns a useless error
    message. Caller is responsible for deleting the file after use.
    """
    fd, name = tempfile.mkstemp(suffix=".xml", prefix="apiary_task_")
    path = Path(name)
    # Close fd before opening via Path to avoid double-open on Windows.
    import os  # local import so module-level imports stay lean
    os.close(fd)
    path.write_text(body, encoding="utf-16")
    return path


def _validate_schedule_for_create(schedule: dict) -> None:
    sc_type = (schedule.get("type") or "").lower()
    if sc_type != "daily":
        raise SchedulerError(
            f"schedule type '{sc_type}' not supported by Windows backend; "
            f"only 'daily' is implemented in this release",
        )


def _parse_schedule(row: dict) -> dict:
    """Extract a canonical schedule dict from a schtasks CSV row.

    Only ``daily`` is parsed in this ticket's scope — other schedule
    types round-trip with their raw ``schtasks_type`` and ``start_time``
    strings so comparison still detects drift, but translation back to
    native flags is deferred.
    """
    sc_type = (row.get("Schedule Type") or "").strip().lower()
    start_time = (row.get("Start Time") or "").strip()
    canonical_time = _canonical_time(start_time)
    return {
        "type": sc_type,
        "time": canonical_time,
    }


def _canonical_time(start_time: str) -> str:
    """Normalize schtasks' ``Start Time`` to ``HH:MM`` 24-hour form.

    schtasks reports times like ``2:00:00 AM`` or ``14:30:00``. We strip
    seconds and convert AM/PM to 24-hour so registry comparisons don't
    trip on cosmetic differences.
    """
    if not start_time:
        return ""
    token = start_time.strip().upper()
    ampm: Optional[str] = None
    if token.endswith(" AM") or token.endswith(" PM"):
        ampm = token[-2:]
        token = token[:-3].strip()
    parts = token.split(":")
    if len(parts) < 2:
        return start_time
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return start_time
    if ampm == "AM" and hour == 12:
        hour = 0
    elif ampm == "PM" and hour != 12:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


