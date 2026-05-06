"""Mailbox / forwarding inbox for per-repo drift notifications.

Per MIGRATION-PLAN.md §3.2 (D7–D9), §6.6, §7.4: bootstrapped repos drop
JSON files at ``<main-apiary>/.apiary/forwarding/<uid>.json`` to notify
main-apiary of registry-relevant changes (path moves, new copies). This
module owns the schema and the write/read/delete primitives. Processing
(reading messages and applying them to the registry) is a separate
function so the drift hook never holds the registry lock.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.utils import state
from core.utils.filelock import FileLock

# Mailbox schema version — bumped if the message shape changes.
MAILBOX_SCHEMA_VERSION = 1

# Recognized message kinds.
KIND_UPDATE_PATH = "update_path"
KIND_REGISTER_COPY = "register_copy"


def forwarding_dir(apiary: Path) -> Path:
    """Return ``<apiary>/.apiary/forwarding/`` — the mailbox directory."""
    return Path(apiary) / ".apiary" / "forwarding"


def message_path(apiary: Path, from_uid: int) -> Path:
    """Return the mailbox file path for *from_uid*. One file per uid by
    convention so duplicate messages from the same repo collapse to the
    most-recent claim (intentional — see §9.6)."""
    return forwarding_dir(apiary) / f"{from_uid}.json"


def write_message(
    apiary: Path,
    *,
    from_uid: int,
    kind: str,
    new_path: Path | str,
    name: str,
    version: str,
    old_path: Path | str | None = None,
) -> Path:
    """Atomically write a forwarding message into main-apiary's mailbox.

    Schema (§6.6)::

        {schema_version, from_uid, kind, old_path?, new_path, name, version, ts}

    *kind* must be ``KIND_UPDATE_PATH`` (repo moved) or
    ``KIND_REGISTER_COPY`` (a copy was detected; main-apiary should
    allocate a fresh entry under the supplied name + new_path). Caller
    has already verified main-apiary is reachable per §7.1 — this
    function does not re-check.
    """
    if kind not in (KIND_UPDATE_PATH, KIND_REGISTER_COPY):
        raise ValueError(f"unknown mailbox kind: {kind!r}")
    payload = {
        "schema_version": MAILBOX_SCHEMA_VERSION,
        "from_uid": from_uid,
        "kind": kind,
        "new_path": str(new_path),
        "name": name,
        "version": version,
        "ts": state._now_iso(),
    }
    if old_path is not None:
        payload["old_path"] = str(old_path)
    fdir = forwarding_dir(apiary)
    fdir.mkdir(parents=True, exist_ok=True)
    p = message_path(apiary, from_uid)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def list_pending(apiary: Path) -> list[Path]:
    """Return all pending mailbox files, sorted by uid."""
    fdir = forwarding_dir(apiary)
    if not fdir.is_dir():
        return []
    return sorted(p for p in fdir.glob("*.json") if p.is_file())


def read_message(p: Path) -> dict | None:
    """Parse a single mailbox file. Returns None for malformed messages
    (caller should leave them alone — operator triages via doctor)."""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("kind") not in (KIND_UPDATE_PATH, KIND_REGISTER_COPY):
        return None
    return data


def process_pending(apiary: Path) -> dict:
    """Drain the mailbox under registry FileLock and apply every message.

    Per §7.4: each message updates or extends ``registry.json`` then the
    file is deleted. Mailbox is single-consumer (only main-apiary
    processes it), so no inter-process coordination is needed beyond the
    registry lock.

    Returns a small report dict for the caller (CLI output / tests)::

        {
            "processed": int,
            "applied": [{"uid": int, "kind": str, "new_path": str}, ...],
            "errors":  [{"file": str, "reason": str}, ...],
        }
    """
    pending = list_pending(apiary)
    report: dict = {"processed": 0, "applied": [], "errors": []}
    if not pending:
        return report

    with FileLock(state.registry_path(apiary)):
        registry = state._load_registry(apiary)
        changed = False
        for p in pending:
            msg = read_message(p)
            if msg is None:
                report["errors"].append({"file": str(p), "reason": "malformed"})
                continue
            kind = msg["kind"]
            if kind == KIND_UPDATE_PATH:
                from_uid = str(msg.get("from_uid"))
                if from_uid not in registry:
                    report["errors"].append({
                        "file": str(p),
                        "reason": f"update_path for unknown uid {from_uid}",
                    })
                    continue
                registry[from_uid]["real_path"] = msg["new_path"]
                registry[from_uid]["last_used"] = msg.get("ts", state._now_iso())
                changed = True
                report["applied"].append({
                    "uid": int(from_uid), "kind": kind, "new_path": msg["new_path"],
                })
            elif kind == KIND_REGISTER_COPY:
                # The copy already wrote its own self-pointer with the
                # newly allocated uid; we just record the registry entry.
                from_uid = int(msg["from_uid"])
                registry[str(from_uid)] = {
                    "name": msg["name"],
                    "real_path": msg["new_path"],
                    "uid": from_uid,
                    "version": msg["version"],
                    "registered_at": msg.get("ts", state._now_iso()),
                    "last_used": msg.get("ts", state._now_iso()),
                    "verified_ok": True,
                }
                changed = True
                report["applied"].append({
                    "uid": from_uid, "kind": kind, "new_path": msg["new_path"],
                })
            # Delete the message either way so it doesn't get re-processed.
            try:
                p.unlink()
            except OSError:
                pass
            report["processed"] += 1
        if changed:
            state._save_registry(apiary, registry)
    return report
