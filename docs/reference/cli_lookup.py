#!/usr/bin/env python3
"""Look up full CLI reference for a specific tool.

Usage:
    python docs/reference/cli_lookup.py <query>

Examples:
    python docs/reference/cli_lookup.py notes.py
    python docs/reference/cli_lookup.py round_counter
    python docs/reference/cli_lookup.py report
"""
import sys
from pathlib import Path

REFERENCE = Path(__file__).resolve().parent / "cli-tools.md"


def list_known_tools() -> list[str]:
    """Return the list of known repo CLI tool paths from cli-tools.md."""
    if not REFERENCE.exists():
        return []
    text = REFERENCE.read_text(encoding="utf-8")
    tools = []
    for line in text.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            header = line[3:].strip()
            if not header:
                continue
            if header == "docs/reference/cli_lookup.py":
                continue
            if Path(header).name == "cli_lookup.py":
                continue
            tools.append(header)
    return list(dict.fromkeys(tools))


def main():
    if len(sys.argv) < 2:
        print("Usage: cli_lookup.py <tool-name>", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1].lower()

    if not REFERENCE.exists():
        print(f"Error: {REFERENCE} not found", file=sys.stderr)
        sys.exit(1)

    text = REFERENCE.read_text(encoding="utf-8")

    # Split into sections by ## headers (top-level tool sections)
    sections: list[tuple[str, str]] = []
    current_header = ""
    current_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            if current_header:
                sections.append((current_header, "\n".join(current_lines)))
            current_header = line
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_header:
        sections.append((current_header, "\n".join(current_lines)))

    # Match query against section headers
    matches = []
    for header, body in sections:
        if query in header.lower():
            matches.append(body.strip())

    if not matches:
        print(f"No tool matching '{query}'", file=sys.stderr)
        sys.exit(1)

    print("\n\n".join(matches))


if __name__ == "__main__":
    main()
