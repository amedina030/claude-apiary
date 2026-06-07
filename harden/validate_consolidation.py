#!/usr/bin/env python3
"""
Validate Consolidator (referee) output from the multi-lens harden flow.

Reads a JSON object from stdin (or --file) with "accepted" and "rejected"
arrays. The consolidator dedups overlapping findings across the per-lens
attackers, then adjudicates accept/reject with a default-accept posture. This
validator enforces the output shape, the severity enum, that every dispatched
source finding is accounted for exactly once (when --source-ids is given), and
optional file existence for accepted locations.

Also provides degrade_dedup(): the deterministic fallback used when the
consolidator LLM step fails twice — it merges the raw per-lens findings by
location (keeping the highest severity) so the defender still receives a clean,
deduped set with no adjudication.

Exit 0 + prints validated JSON on success.
Exit 1 + prints error details on failure.

Usage:
    validate_consolidation.py --file consolidation.json [--source-ids ATK-SEC-001,ATK-COR-002] [--check-files]
    validate_consolidation.py --degrade --file merged_findings.json   # deterministic fallback
"""
import argparse
import json
import sys
from pathlib import Path

from validate_common import check_path_escape, read_json_input, report_errors
from validate_findings import _resolve_filepath

VALID_SEVERITIES = {"critical", "high", "medium", "low"}
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

MAX_ACCEPTED = 1000  # bound output size, mirror findings/response caps
MAX_REJECTED = 1000


def _validate_source_ids(value, label: str, errors: list) -> list:
    """Validate a source_ids field is a non-empty list of non-empty strings.
    Returns the list of valid string ids found (for coverage bookkeeping)."""
    if value is None:
        errors.append(f"{label}: missing required field 'source_ids'")
        return []
    if not isinstance(value, list):
        errors.append(f"{label}: 'source_ids' must be a JSON array, got {type(value).__name__}")
        return []
    if not value:
        errors.append(f"{label}: 'source_ids' must not be empty")
        return []
    ids = []
    for sid in value:
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"{label}: each source_id must be a non-empty string")
        else:
            ids.append(sid)
    return ids


def validate(data: dict, source_ids: set = None, check_files: bool = False) -> list[str]:
    """Validate consolidator output and return a list of error strings (empty = valid)."""
    errors = []

    if not isinstance(data, dict):
        return ["Expected a JSON object with 'accepted' and 'rejected' arrays"]

    accepted = data.get("accepted")
    rejected = data.get("rejected")
    if accepted is None and rejected is None:
        return ["Missing both 'accepted' and 'rejected' — expected at least one array"]
    if accepted is None:
        accepted = []
    if rejected is None:
        rejected = []
    if not isinstance(accepted, list):
        return ["'accepted' must be a JSON array"]
    if not isinstance(rejected, list):
        return ["'rejected' must be a JSON array"]
    if len(accepted) > MAX_ACCEPTED:
        return [f"Too many accepted findings: {len(accepted)} (maximum {MAX_ACCEPTED})"]
    if len(rejected) > MAX_REJECTED:
        return [f"Too many rejected findings: {len(rejected)} (maximum {MAX_REJECTED})"]

    seen_sources: dict[str, str] = {}  # source_id -> where it first appeared

    def _track(ids: list, where: str) -> None:
        for sid in ids:
            if sid in seen_sources:
                errors.append(
                    f"source_id '{sid}' referenced more than once "
                    f"(in {seen_sources[sid]} and {where}) — each finding is "
                    f"deduped into exactly one accepted or rejected entry"
                )
            else:
                seen_sources[sid] = where

    for i, item in enumerate(accepted):
        if not isinstance(item, dict):
            errors.append(f"accepted[{i}]: not a JSON object")
            continue
        label = f"accepted[{i}]"

        if "id" in item:
            errors.append(f"{label}: accepted findings must not include an 'id' field (assigned by post-processor)")

        for field in ("description", "severity", "location"):
            val = item.get(field)
            if val is None:
                errors.append(f"{label}: missing required field '{field}'")
            elif not isinstance(val, str):
                errors.append(f"{label}: field '{field}' must be a string, got {type(val).__name__}")
            elif not val.strip():
                errors.append(f"{label}: field '{field}' is empty")

        severity = item.get("severity")
        if isinstance(severity, str) and severity not in VALID_SEVERITIES:
            errors.append(f"{label}: invalid severity '{severity}' (expected: {', '.join(sorted(VALID_SEVERITIES))})")

        ids = _validate_source_ids(item.get("source_ids"), label, errors)
        _track(ids, label)

        # 'lenses' is optional but if present must be a list of strings
        lenses = item.get("lenses")
        if lenses is not None:
            if not isinstance(lenses, list):
                errors.append(f"{label}: 'lenses' must be a JSON array, got {type(lenses).__name__}")
            elif not all(isinstance(x, str) and x.strip() for x in lenses):
                errors.append(f"{label}: each entry in 'lenses' must be a non-empty string")

        if check_files:
            location = item.get("location", "")
            if isinstance(location, str) and location:
                if "," in location:
                    errors.append(f"{label}: 'location' must reference a single file, got multi-file: {location}")
                else:
                    filepath = _resolve_filepath(location)
                    escape_err = check_path_escape(filepath)
                    if escape_err:
                        errors.append(f"{label}: location {escape_err}")
                    elif not Path(filepath).resolve().exists():
                        errors.append(f"{label}: file not found: {filepath}")

    for i, item in enumerate(rejected):
        if not isinstance(item, dict):
            errors.append(f"rejected[{i}]: not a JSON object")
            continue
        label = f"rejected[{i}]"
        ids = _validate_source_ids(item.get("source_ids"), label, errors)
        _track(ids, label)
        reason = item.get("reason")
        if reason is None:
            errors.append(f"{label}: missing required field 'reason' (default-accept means a rejection must be justified)")
        elif not isinstance(reason, str):
            errors.append(f"{label}: 'reason' must be a string, got {type(reason).__name__}")
        elif not reason.strip():
            errors.append(f"{label}: 'reason' is empty")

    # Coverage: every dispatched source finding must be accounted for exactly
    # once, and no unknown source ids may be invented.
    if source_ids is not None:
        referenced = set(seen_sources)
        missing = source_ids - referenced
        for sid in sorted(missing):
            errors.append(f"source finding {sid} not accounted for in accepted or rejected")
        unknown = referenced - source_ids
        for sid in sorted(unknown):
            errors.append(f"unknown source id {sid} not among the dispatched findings")

    return errors


def degrade_dedup(findings: list) -> list:
    """Deterministic fallback when the consolidator LLM fails twice.

    Merge per-lens attacker findings (each with an 'id', 'location', 'severity',
    'description', and 'lens') by location, keeping the highest severity and
    collecting the contributing source ids and lenses. Returns an 'accepted'
    list (no rejections) ready for CON-NNN assignment.
    """
    by_location: dict[str, dict] = {}
    order: list[str] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        loc = f.get("location", "")
        sev = f.get("severity", "low")
        fid = f.get("id")
        lens = f.get("lens")
        if loc not in by_location:
            by_location[loc] = {
                "description": f.get("description", ""),
                "severity": sev,
                "location": loc,
                "source_ids": [],
                "lenses": [],
            }
            order.append(loc)
        entry = by_location[loc]
        if fid:
            entry["source_ids"].append(fid)
        if lens and lens not in entry["lenses"]:
            entry["lenses"].append(lens)
        # keep the highest severity seen at this location, and its description
        if SEVERITY_RANK.get(sev, 0) > SEVERITY_RANK.get(entry["severity"], 0):
            entry["severity"] = sev
            entry["description"] = f.get("description", entry["description"])
    return [by_location[loc] for loc in order]


def main():
    parser = argparse.ArgumentParser(description="Validate harden Consolidator output")
    parser.add_argument("--source-ids",
                        help="Comma-separated ATK-<CODE>-NNN ids dispatched to the consolidator; "
                             "enables exact coverage checking")
    parser.add_argument("--check-files", action="store_true",
                        help="Verify that accepted-finding files exist")
    parser.add_argument("--degrade", action="store_true",
                        help="Fallback mode: dedup raw merged findings by location instead of validating")
    parser.add_argument("--file", dest="file_path",
                        help="Read JSON from file instead of stdin")
    args = parser.parse_args()

    if args.degrade:
        _raw, findings = read_json_input(file_path=args.file_path)
        if not isinstance(findings, list):
            print("ERROR: --degrade expects a JSON array of merged findings", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"accepted": degrade_dedup(findings), "rejected": []}, indent=2))
        return

    source_ids = None
    if args.source_ids:
        source_ids = {s.strip() for s in args.source_ids.split(",") if s.strip()}
        if not source_ids:
            print("ERROR: --source-ids must contain at least one non-empty id", file=sys.stderr)
            sys.exit(1)

    raw, data = read_json_input(file_path=args.file_path)
    errors = validate(data, source_ids=source_ids, check_files=args.check_files)
    report_errors(errors)
    print(raw)


if __name__ == "__main__":
    main()
