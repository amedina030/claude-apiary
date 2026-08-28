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


USAGE = """usage: cli_lookup.py <query>

Print the cli-tools.md section for a tool. The query is matched as a
case-insensitive substring against each section's `## ` header, so a bare
name works as well as a path.

positional arguments:
  query       tool name or path fragment (e.g. notes.py, round_counter, report)

options:
  -h, --help  show this help message and exit
  --list      list every known tool header and exit
"""


def main():
    argv = sys.argv[1:]
    # Hand-rolled rather than argparse: `--help` has to answer without reading
    # cli-tools.md, and a bare `--help` used to fall through to the substring
    # match and exit 1 with "No tool matching '--help'" (review §4). Every
    # documented command in the repo is run with `--help` by
    # docs/test_doc_examples.py, so this one has to mean what it says.
    if not argv:
        print(USAGE, file=sys.stderr)
        sys.exit(1)
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0)
    if argv[0] == "--list":
        for tool in list_known_tools():
            print(tool)
        sys.exit(0)

    query = argv[0].lower()

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
