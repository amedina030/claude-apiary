#!/usr/bin/env python3
"""
Harden validate-and-assign: validate then assign IDs in a single step.

Combines validate_findings/validate_response + assign_ids into one call,
eliminating the risk of running them in the wrong order.

Usage:
    echo '<json>' | validate_and_assign.py findings [--check-files] [--deep]
    echo '<json>' | validate_and_assign.py response --expected-ids ATK-001,ATK-002 [--check-files]
    validate_and_assign.py findings --file findings.json [--check-files] [--deep]
    validate_and_assign.py response --file response.json --expected-ids ATK-001,ATK-002 [--check-files]
"""
import argparse
import json
import sys

from assign_ids import assign_ids
from lenses import LENSES, code_for, is_valid_lens
from validate_common import read_json_input, report_errors
from validate_consolidation import degrade_dedup
from validate_consolidation import validate as validate_consolidation
from validate_findings import sanitize as sanitize_findings
from validate_findings import validate as validate_findings
from validate_response import validate as validate_response


def main():
    parser = argparse.ArgumentParser(description="Harden: validate + assign IDs")
    sub = parser.add_subparsers(dest="command")

    p_findings = sub.add_parser("findings", help="Validate and assign IDs to Attacker findings")
    p_findings.add_argument("--check-files", action="store_true")
    p_findings.add_argument("--deep", action="store_true")
    p_findings.add_argument("--file", dest="file_path")
    p_findings.add_argument("--sanitize", action="store_true",
                            help="Auto-fix common issues before validation")
    p_findings.add_argument("--lens",
                            help="Per-lens attacker mode: validate against the lens "
                                 "vocab and assign ATK-<CODE>-NNN IDs")

    p_response = sub.add_parser("response", help="Validate and assign IDs to Defender response")
    p_response.add_argument("--expected-ids", required=True)
    p_response.add_argument("--check-files", action="store_true")
    p_response.add_argument("--file", dest="file_path")

    p_consolidation = sub.add_parser(
        "consolidation", help="Validate Consolidator output and assign CON-NNN to accepted findings")
    p_consolidation.add_argument("--source-ids",
                                 help="Comma-separated ATK-<CODE>-NNN ids dispatched to the consolidator")
    p_consolidation.add_argument("--check-files", action="store_true")
    p_consolidation.add_argument("--file", dest="file_path")
    p_consolidation.add_argument("--degrade", action="store_true",
                                 help="Fallback: dedup raw merged findings by location, then assign CON-NNN")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "findings":
        if args.lens is not None and not is_valid_lens(args.lens):
            print(f"ERROR: invalid lens '{args.lens}' (expected one of: "
                  f"{', '.join(sorted(LENSES))})", file=sys.stderr)
            sys.exit(1)
        raw, data = read_json_input(file_path=args.file_path)
        if args.sanitize:
            data = sanitize_findings(data, deep=args.deep, lens=args.lens)
        errors = validate_findings(data, check_files=args.check_files, deep=args.deep,
                                   lens=args.lens)
        report_errors(errors)
        prefix = f"ATK-{code_for(args.lens)}" if args.lens else "ATK"
        result = assign_ids(data, prefix)
        print(json.dumps(result, indent=2))

    elif args.command == "response":
        expected = {id_.strip() for id_ in args.expected_ids.split(",") if id_.strip()}
        if not expected:
            print("ERROR: --expected-ids must contain at least one non-empty ATK-ID", file=sys.stderr)
            sys.exit(1)

        raw, data = read_json_input(file_path=args.file_path)
        # Extract responses array, validate full object, assign IDs to responses
        errors = validate_response(data, expected_ids=expected, check_files=args.check_files)
        report_errors(errors)
        data["responses"] = assign_ids(data["responses"], "DEF")
        print(json.dumps(data, indent=2))

    elif args.command == "consolidation":
        # Degrade fallback: input is the raw merged per-lens findings; dedup them
        # deterministically by location, then assign CON-NNN. No adjudication.
        if args.degrade:
            _raw, findings = read_json_input(file_path=args.file_path)
            if not isinstance(findings, list):
                print("ERROR: --degrade expects a JSON array of merged findings", file=sys.stderr)
                sys.exit(1)
            accepted = assign_ids(degrade_dedup(findings), "CON")
            print(json.dumps({"accepted": accepted, "rejected": []}, indent=2))
            return

        source_ids = None
        if args.source_ids:
            source_ids = {s.strip() for s in args.source_ids.split(",") if s.strip()}
            if not source_ids:
                print("ERROR: --source-ids must contain at least one non-empty id", file=sys.stderr)
                sys.exit(1)

        raw, data = read_json_input(file_path=args.file_path)
        errors = validate_consolidation(data, source_ids=source_ids, check_files=args.check_files)
        report_errors(errors)
        data["accepted"] = assign_ids(data.get("accepted", []), "CON")
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
