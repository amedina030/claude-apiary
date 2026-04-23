"""Observer failure-path harness.

Wraps observer script handlers with the invariant: any failure is logged
to ``<target>/.apiary/observer.log`` and filed as a scribe blocker note,
but the observer process always exits 0 so the originating tool call is
never blocked (:ac:`AC-11`).

Successful dispatches that produce a summary dict are appended to a
per-session JSONL under ``<target>/.apiary/observer/<YYYY-MM-DD>/<session>.jsonl``.
Scribe is reserved for the human-attention failure path only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .adapter import (
    AdapterError,
    ObservationEvent,
    _MAX_STDIN_BYTES,
    _parse_json_bytes,
    from_payload,
)
from core.utils.filelock import FileLock

_LAUNCHER_PATH = Path.home() / ".claude" / "apiary_launch.py"
_SCRIBE_TIMEOUT_SECONDS = 15
_UNKNOWN_SESSION = "unknown-session"


ObserverHandler = Callable[[ObservationEvent], Optional[dict]]


def run_observer(observer_name: str, handler: ObserverHandler) -> int:
    """Read stdin, build an event, dispatch to ``handler``. Never raise.

    Return code is always 0 by contract — observer failures must not block
    the originating tool call. Failures are persisted to
    ``<cwd>/.apiary/observer.log`` and surfaced as a scribe blocker note so
    silent observer breakage becomes visible at next session startup.

    The handler may return a ``dict`` summary to record the observation in
    ``<cwd>/.apiary/observer/<date>/<session>.jsonl`` (one line per call).
    Returning ``None`` signals "not interested in this event" and skips the
    log write — defense-in-depth matchers do this when the tool name
    doesn't match the observer's scope.
    """
    target_repo = Path.cwd()
    raw_payload: Optional[Any] = None

    try:
        raw_bytes = _read_stdin_preserving_raw()
        if not raw_bytes:
            raise AdapterError("stdin is empty")
        raw_payload = _parse_json_bytes(raw_bytes)
        event = from_payload(raw_payload)
        summary = handler(event)
        if summary is not None:
            _append_observation_log(target_repo, observer_name, event, summary)
        return 0
    except Exception as exc:  # noqa: BLE001 — contract: never propagate
        _handle_failure(target_repo, observer_name, exc, raw_payload)
        return 0


def _read_stdin_preserving_raw() -> bytes:
    try:
        return sys.stdin.buffer.read(_MAX_STDIN_BYTES)
    except OSError:
        return b""


def _handle_failure(
    target_repo: Path,
    observer_name: str,
    exc: BaseException,
    raw_payload: Optional[Any],
) -> None:
    """Log + file blocker. Swallow any secondary failure."""
    error_text = _format_exception(exc)
    summary_line = _summary_line(exc)
    try:
        _append_observer_log(target_repo, observer_name, error_text, raw_payload)
    except Exception:  # noqa: BLE001 — never let logging crash the observer
        pass
    try:
        _file_scribe_blocker(observer_name, summary_line, error_text, raw_payload)
    except Exception:  # noqa: BLE001 — scribe unreachable must not propagate
        pass


def _append_observation_log(
    target_repo: Path,
    observer_name: str,
    event: ObservationEvent,
    summary: dict,
) -> None:
    """Append one JSONL line per observed event to the per-session log.

    Path shape: ``<target>/.apiary/observer/<YYYY-MM-DD>/<session>.jsonl``.
    Date comes from wall-clock UTC; session comes from the event. Lines are
    newline-delimited JSON; ``default=str`` accommodates non-serialisable
    values in ``tool_response``.
    """
    now = datetime.now(timezone.utc)
    session_id = event.session_id or _UNKNOWN_SESSION
    log_dir = target_repo / ".apiary" / "observer" / now.strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{session_id}.jsonl"
    entry = {
        "timestamp": now.isoformat(),
        "observer": observer_name,
        "schema_version": event.schema_version,
        "event": _event_to_dict(event),
        "summary": summary,
    }
    line = json.dumps(entry, default=str, ensure_ascii=False)
    with FileLock(log_path):
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _event_to_dict(event: ObservationEvent) -> dict:
    return {
        "session_id": event.session_id,
        "hook_event_name": event.hook_event_name,
        "tool_name": event.tool_name,
        "tool_use_id": event.tool_use_id,
        "transcript_path": event.transcript_path,
        "cwd": event.cwd,
        "permission_mode": event.permission_mode,
        "raw": {
            "tool_input": event.raw.tool_input,
            "tool_response": event.raw.tool_response,
        },
    }


def _append_observer_log(
    target_repo: Path,
    observer_name: str,
    error_text: str,
    raw_payload: Optional[Any],
) -> None:
    log_dir = target_repo / ".apiary"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "observer.log"
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        raw_repr = json.dumps(raw_payload, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        raw_repr = "<unserializable>"
    entry = (
        f"[{timestamp}] observer={observer_name}\n"
        f"{error_text.rstrip()}\n"
        f"raw_payload={raw_repr}\n"
        f"---\n"
    )
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(entry)


def _file_scribe_blocker(
    observer_name: str,
    summary: str,
    error_text: str,
    raw_payload: Optional[Any],
) -> None:
    if not _LAUNCHER_PATH.is_file():
        return
    try:
        raw_repr = json.dumps(raw_payload, default=str, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        raw_repr = "<unserializable>"
    content = (
        f"Observer **{observer_name}** failed during a PostToolUse dispatch.\n\n"
        f"```\n{error_text.rstrip()}\n```\n\n"
        f"**Raw payload:**\n```json\n{raw_repr}\n```\n\n"
        f"See `.apiary/observer.log` in the target repo for the full history."
    )
    subprocess.run(
        [
            sys.executable,
            str(_LAUNCHER_PATH),
            "scribe/notes.py",
            "add",
            "--type",
            "blocker",
            "--summary",
            summary,
            "--content",
            content,
            "--auto",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_SCRIBE_TIMEOUT_SECONDS,
    )


def _format_exception(exc: BaseException) -> str:
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )


def _summary_line(exc: BaseException) -> str:
    cls = type(exc).__name__
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if msg:
        text = f"observer failure: {cls}: {msg}"
    else:
        text = f"observer failure: {cls}"
    return text[:140]
