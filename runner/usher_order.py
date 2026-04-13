#!/usr/bin/env python3
"""
Usher order — manifest of tickets eligible for runner pickup.

Manages usher_order.json: registration, dependency-aware selection,
status updates, failure cascading, and archival.
"""
from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from typing import Optional

from core.utils.filelock import FileLock

SCRIPT_DIR = Path(__file__).resolve().parent
ORDER_FILE = SCRIPT_DIR / "usher_order.json"
ORDER_ARCHIVE_DIR = SCRIPT_DIR / "usher_order_archive"

_EMPTY_ORDER = {"groups": [], "standalone": []}

VALID_STATUSES = frozenset({"pending", "running", "completed", "failed", "blocked"})


def load_order(path: Optional[Path] = None) -> dict:
    """Load usher_order.json. Returns empty structure if missing or malformed."""
    target = path or ORDER_FILE
    if not target.exists():
        return {"groups": [], "standalone": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"groups": [], "standalone": []}
        data.setdefault("groups", [])
        data.setdefault("standalone", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"groups": [], "standalone": []}


def save_order(order: dict, path: Optional[Path] = None) -> None:
    """Atomically write usher_order.json under file lock."""
    target = path or ORDER_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    with FileLock(target):
        tmp.write_text(json.dumps(order, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)


def register_standalone(uuid: str, slug: str, path: Optional[Path] = None) -> None:
    """Add a standalone ticket to the order file. Deduplicates by uuid."""
    order = load_order(path)
    if any(t["uuid"] == uuid for t in order["standalone"]):
        return
    order["standalone"].append({
        "uuid": uuid,
        "slug": slug,
        "status": "pending",
    })
    save_order(order, path)


def register_group(group_id: str, origin: str, tickets: list[dict],
                   path: Optional[Path] = None) -> None:
    """Add a group of dependent tickets to the order file.

    Each ticket dict must have: uuid, slug, depends_on (list of uuids).
    All tickets start with status=pending.
    """
    order = load_order(path)
    group_entry = {
        "group_id": group_id,
        "origin": origin,
        "tickets": [],
    }
    for t in tickets:
        group_entry["tickets"].append({
            "uuid": t["uuid"],
            "slug": t["slug"],
            "depends_on": t.get("depends_on", []),
            "status": "pending",
        })
    order["groups"].append(group_entry)
    save_order(order, path)


def _all_tickets(order: dict):
    """Yield (ticket_dict, group_or_none) for every ticket in the order."""
    for t in order["standalone"]:
        yield t, None
    for g in order["groups"]:
        for t in g["tickets"]:
            yield t, g


def _status_of(uuid: str, order: dict) -> Optional[str]:
    """Return the status of a ticket by uuid, or None if not found."""
    for t, _ in _all_tickets(order):
        if t["uuid"] == uuid:
            return t["status"]
    return None


def next_eligible(path: Optional[Path] = None) -> Optional[dict]:
    """Find the first pending ticket whose dependencies are all completed.

    Checks standalone tickets first, then groups in order.
    Returns the ticket dict (augmented with group_id if from a group), or None.
    """
    order = load_order(path)

    # Standalone tickets first (no dependencies)
    for t in order["standalone"]:
        if t["status"] == "pending":
            return dict(t)

    # Group tickets: find first pending/blocked with all deps completed.
    # "blocked" tickets auto-unblock when their dependencies complete —
    # no separate cascade_success step needed.
    for g in order["groups"]:
        for t in g["tickets"]:
            if t["status"] not in ("pending", "blocked"):
                continue
            deps_met = all(
                _status_of(dep_uuid, order) == "completed"
                for dep_uuid in t.get("depends_on", [])
            )
            if deps_met:
                result = dict(t)
                result["group_id"] = g["group_id"]
                return result

    return None


def update_status(uuid: str, new_status: str,
                  path: Optional[Path] = None) -> bool:
    """Update a ticket's status. Returns True if found and updated."""
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {new_status!r}. Must be one of {VALID_STATUSES}")
    order = load_order(path)
    for t, _ in _all_tickets(order):
        if t["uuid"] == uuid:
            t["status"] = new_status
            save_order(order, path)
            return True
    return False


def cascade_failure(uuid: str, path: Optional[Path] = None) -> list[str]:
    """Mark all transitive dependents of a failed ticket as blocked.

    Only cascades within the same group. Returns list of blocked uuids.
    """
    order = load_order(path)

    # Find the group containing this uuid
    target_group = None
    for g in order["groups"]:
        for t in g["tickets"]:
            if t["uuid"] == uuid:
                target_group = g
                break
        if target_group is not None:
            break

    if target_group is None:
        return []

    # Build set of failed/blocked uuids and propagate transitively
    blocked_uuids: set[str] = {uuid}
    changed = True
    while changed:
        changed = False
        for t in target_group["tickets"]:
            if t["status"] == "blocked" or t["uuid"] in blocked_uuids:
                continue
            if any(dep in blocked_uuids for dep in t.get("depends_on", [])):
                t["status"] = "blocked"
                blocked_uuids.add(t["uuid"])
                changed = True

    # Don't include the original failed uuid in the return list
    blocked_uuids.discard(uuid)
    if blocked_uuids:
        save_order(order, path)
    return sorted(blocked_uuids)


def archive_order(path: Optional[Path] = None) -> Optional[Path]:
    """Move current order file to usher_order_archive/ with date suffix."""
    target = path or ORDER_FILE
    if not target.exists():
        return None

    archive_dir = ORDER_ARCHIVE_DIR if path is None else target.parent / "usher_order_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    date_suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%Y_%m_%d")
    dest = archive_dir / f"usher_order_{date_suffix}.json"

    # If same-day archive exists, add time
    if dest.exists():
        time_suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%H%M%S")
        dest = archive_dir / f"usher_order_{date_suffix}_{time_suffix}.json"

    shutil.move(str(target), str(dest))
    return dest


def detect_stale_running(path: Optional[Path] = None) -> list[dict]:
    """Return all tickets currently in 'running' status."""
    order = load_order(path)
    stale = []
    for t, g in _all_tickets(order):
        if t["status"] == "running":
            entry = dict(t)
            if g is not None:
                entry["group_id"] = g["group_id"]
            stale.append(entry)
    return stale
