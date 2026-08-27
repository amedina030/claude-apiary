#!/usr/bin/env python3
"""
Shared 7-lens taxonomy for the multi-lens harden flow.

Single source of truth for the lens vocabulary, their short ID codes (used in
ATK-<CODE>-NNN finding IDs), and one-line briefs. Imported by the validators,
the ID assigner, and read by the orchestrator (via `lenses.py list`) so the
skill never hard-codes the list in more than one place.

The taxonomy and the seam rules below mirror spec note C-2026-48.
"""

import argparse
import json
import sys

# lens name -> 3-letter ID code. Codes are distinct so ATK-<CODE>-NNN never
# collides across lenses. Order is the canonical fan-out order.
LENSES: dict[str, str] = {
    "correctness": "COR",
    "security": "SEC",
    "robustness": "ROB",
    "resilience": "RES",
    "complexity": "CPX",
    "architecture": "ARC",
    "testing": "TST",
}

# One-line brief per lens, handed to that lens's attacker so each specialist
# stays in its lane. Seam rules (below) disambiguate the overlapping pairs.
LENS_BRIEFS: dict[str, str] = {
    "correctness": (
        "Logic bugs, off-by-one, boolean/operator errors, control flow, wrong "
        "algorithm, edge cases mishandled in single-threaded code."
    ),
    "security": (
        "Authz/authn, injection, path traversal, secrets, PII, unsafe "
        "deserialization. Threat model = a hostile actor."
    ),
    "robustness": (
        "Ingestion of untrusted/external data: parsing, deserialization, "
        "perimeter validation, malformed payloads, encoding/format surprises."
    ),
    "resilience": (
        "Error handling, exception paths, resource leaks, retries/recovery, "
        "concurrency (races, locks, shared state, deadlock)."
    ),
    "complexity": ("Convoluted local logic, dead code, within-unit duplication, over-engineering."),
    "architecture": (
        "Module boundaries, coupling, dependency direction, pattern "
        "consistency, cross-module duplication, scale-if-repeated."
    ),
    "testing": "Coverage gaps, weak/missing assertions, test anti-patterns.",
}

# Seam rules: how to resolve the lenses that look like they overlap.
SEAM_RULES: str = (
    "security vs robustness = threat model (malicious vs benign-malformed); "
    "complexity vs architecture = scope (within-unit vs cross-unit); "
    "correctness vs resilience = condition (wrong single-threaded vs "
    "only-fails-under-failure/concurrency)."
)


def is_valid_lens(name: str) -> bool:
    return isinstance(name, str) and name.lower() in LENSES


def code_for(name: str) -> str:
    """Return the 3-letter ID code for a lens name (case-insensitive)."""
    return LENSES[name.lower()]


def all_lenses() -> list[str]:
    return list(LENSES.keys())


def main() -> None:
    parser = argparse.ArgumentParser(description="Harden 7-lens taxonomy")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="Print the canonical lens names, one per line")
    sub.add_parser("codes", help="Print 'name=CODE' pairs, one per line")
    sub.add_parser("json", help="Print the full taxonomy (names, codes, briefs, seams) as JSON")

    args = parser.parse_args()
    if args.command == "codes":
        for name, code in LENSES.items():
            print(f"{name}={code}")
    elif args.command == "json":
        print(
            json.dumps(
                {
                    "lenses": LENSES,
                    "briefs": LENS_BRIEFS,
                    "seam_rules": SEAM_RULES,
                },
                indent=2,
            )
        )
    elif args.command == "list":
        for name in LENSES:
            print(name)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
