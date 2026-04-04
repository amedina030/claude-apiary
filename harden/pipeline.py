#!/usr/bin/env python3
"""
Harden pipeline: validate then assign IDs in a single step.

Combines validate_findings/validate_response + assign_ids into one call,
eliminating the risk of running them in the wrong order.

Usage:
    echo '<json>' | pipeline.py findings [--check-files] [--deep]
    echo '<json>' | pipeline.py response --expected-ids ATK-001,ATK-002 [--check-files]
    pipeline.py findings --file findings.json [--check-files] [--deep]
    pipeline.py response --file response.json --expected-ids ATK-001,ATK-002 [--check-files]
"""
import argparse
import json
import sys

from assign_ids import assign_ids
from validate_common import read_json_input, report_errors
from validate_findings import sanitize as sanitize_findings
from validate_findings import validate as validate_findings
from validate_response import validate as validate_response


def main():
    parser = argparse.ArgumentParser(description="Harden pipeline: validate + assign IDs")
    sub = parser.add_subparsers(dest="command")

    p_findings = sub.add_parser("findings", help="Validate and assign IDs to Attacker findings")
    p_findings.add_argument("--check-files", action="store_true")
    p_findings.add_argument("--deep", action="store_true")
    p_findings.add_argument("--file", dest="file_path")
    p_findings.add_argument("--sanitize", action="store_true",
                            help="Auto-fix common issues before validation")

    p_response = sub.add_parser("response", help="Validate and assign IDs to Defender response")
    p_response.add_argument("--expected-ids", required=True)
    p_response.add_argument("--check-files", action="store_true")
    p_response.add_argument("--file", dest="file_path")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "findings":
        raw, data = read_json_input(file_path=args.file_path)
        if args.sanitize:
            data = sanitize_findings(data, deep=args.deep)
        errors = validate_findings(data, check_files=args.check_files, deep=args.deep)
        report_errors(errors)
        result = assign_ids(data, "ATK")
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


if __name__ == "__main__":
    main()
