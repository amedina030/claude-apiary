#!/usr/bin/env python3
"""Documentation framework conformance checker.

Validates that all docs under docs/ conform to the current framework version
defined in docs/_framework.md. Checks:
- Required frontmatter fields are present
- framework_version matches current version
- No unexpected doc types
- Known tool directories have corresponding reference docs
- Hook scripts are documented in hooks.md
- Command files are documented in slash-commands.md
- CLI entry points are documented in cli-tools.md
- All docs have entries in _index.md

Issues are classified as errors or warnings:
- Errors: broken docs (missing frontmatter, invalid type, missing required fields)
- Warnings: coverage gaps, stale versions, missing index entries

Exit codes:
  0 — no errors (warnings may be present)
  1 — errors found (or --strict and any issues found)
"""

import argparse
import os
import re
import sys
from pathlib import Path

DOCS_DIR = Path(__file__).parent
REPO_ROOT = DOCS_DIR.parent
FRAMEWORK_FILE = DOCS_DIR / "_framework.md"
INDEX_FILE = DOCS_DIR / "_index.md"
SKIP_FILES = {"_framework.md", "_index.md"}
SKIP_DIRS = {"commands", "hooks"}  # Not docs — command defs and hook scripts

VALID_TYPES = {"reference", "architecture", "standard", "guide"}
VALID_SCOPES = {"budgeter", "clarifier", "scribe", "core", "project", "docs"}
REQUIRED_FIELDS = {"type", "title", "scope", "description", "framework_version", "last_verified"}

# Top-level tool dirs that should have at least one reference doc mentioning them
KNOWN_TOOLS = {"budgeter", "clarifier", "scribe", "core"}


def parse_frontmatter(path: Path) -> dict | None:
    """Extract YAML frontmatter from a markdown file as a dict."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return None
    fm = {}
    for line in match.group(1).strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip().strip('"').strip("'")
    return fm


def get_framework_version() -> str:
    """Read the current framework version from _framework.md."""
    fm = parse_frontmatter(FRAMEWORK_FILE)
    if not fm or "version" not in fm:
        print("ERROR: _framework.md missing version in frontmatter")
        sys.exit(1)
    return fm["version"]


def find_docs() -> list[Path]:
    """Find all markdown files under docs/ that should be checked."""
    docs = []
    for root, _dirs, files in os.walk(DOCS_DIR):
        for f in files:
            if not f.endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(root, f), DOCS_DIR)
            rel_posix = rel.replace("\\", "/")
            if rel_posix in SKIP_FILES:
                continue
            # Skip non-doc directories (commands, hooks)
            parts = rel_posix.split("/")
            if parts[0] in SKIP_DIRS:
                continue
            docs.append(Path(root) / f)
    return sorted(docs)


def check_doc(path: Path, framework_version: str) -> tuple[list[str], list[str]]:
    """Check a single doc for conformance. Returns (errors, warnings)."""
    errors = []
    warnings = []
    rel = path.relative_to(DOCS_DIR).as_posix()

    fm = parse_frontmatter(path)
    if fm is None:
        errors.append(f"{rel}: missing frontmatter block")
        return errors, warnings

    # Errors: broken doc structure
    missing = REQUIRED_FIELDS - set(fm.keys())
    if missing:
        errors.append(f"{rel}: missing required fields: {', '.join(sorted(missing))}")

    doc_type = fm.get("type", "")
    if doc_type and doc_type not in VALID_TYPES:
        errors.append(f"{rel}: invalid type '{doc_type}' (expected: {', '.join(sorted(VALID_TYPES))})")

    scope = fm.get("scope", "")
    if scope and scope not in VALID_SCOPES:
        errors.append(f"{rel}: invalid scope '{scope}' (expected: {', '.join(sorted(VALID_SCOPES))})")

    lv = fm.get("last_verified", "")
    if lv and not re.match(r"^\d{4}-\d{2}-\d{2}$", lv):
        errors.append(f"{rel}: last_verified '{lv}' not in YYYY-MM-DD format")

    # Warnings: stale but not broken
    doc_version = fm.get("framework_version", "")
    if doc_version and doc_version != framework_version:
        warnings.append(f"{rel}: framework_version '{doc_version}' behind current '{framework_version}'")

    return errors, warnings


def check_coverage(docs: list[Path]) -> list[str]:
    """Check that known tools have reference docs covering them. Returns warnings."""
    issues = []
    ref_docs = [d for d in docs if "reference" in d.relative_to(DOCS_DIR).as_posix()]

    ref_content = ""
    for d in ref_docs:
        ref_content += d.read_text(encoding="utf-8").lower()

    for tool in KNOWN_TOOLS:
        if tool not in ref_content:
            issues.append(f"coverage: no reference doc mentions '{tool}'")

    return issues


def _read_ref_doc(name: str) -> str:
    """Read a reference doc's content, returning empty string if missing."""
    path = DOCS_DIR / "reference" / name
    if path.exists():
        return path.read_text(encoding="utf-8").lower()
    return ""


def check_hooks_coverage() -> list[str]:
    """Verify every hook script is mentioned in docs/reference/hooks.md."""
    issues = []
    hooks_content = _read_ref_doc("hooks.md")
    if not hooks_content:
        issues.append("coverage: docs/reference/hooks.md not found")
        return issues

    for tool_dir in KNOWN_TOOLS:
        hooks_dir = REPO_ROOT / tool_dir / "hooks"
        if not hooks_dir.is_dir():
            continue
        for py_file in sorted(hooks_dir.glob("*.py")):
            if py_file.name.startswith("test_") or py_file.name == "__init__.py":
                continue
            if py_file.stem not in hooks_content:
                issues.append(f"coverage: hook {tool_dir}/hooks/{py_file.name} not in hooks.md")

    # Also check docs/hooks/ for non-git-hook scripts
    docs_hooks_dir = DOCS_DIR / "hooks"
    if docs_hooks_dir.is_dir():
        for py_file in sorted(docs_hooks_dir.glob("*.py")):
            if py_file.name.startswith("test_") or py_file.name == "__init__.py":
                continue
            if py_file.stem not in hooks_content:
                issues.append(f"coverage: hook docs/hooks/{py_file.name} not in hooks.md")

    return issues


def check_commands_coverage() -> list[str]:
    """Verify every command file is mentioned in docs/reference/slash-commands.md."""
    issues = []
    cmds_content = _read_ref_doc("slash-commands.md")
    if not cmds_content:
        issues.append("coverage: docs/reference/slash-commands.md not found")
        return issues

    for tool_dir in list(KNOWN_TOOLS) + ["docs"]:
        cmd_dir = REPO_ROOT / tool_dir / "commands"
        if not cmd_dir.is_dir():
            continue
        for md_file in sorted(cmd_dir.glob("*.md")):
            # Check for the command name (stem) in the doc
            if md_file.stem not in cmds_content:
                issues.append(f"coverage: command {tool_dir}/commands/{md_file.name} not in slash-commands.md")

    return issues


def check_cli_coverage() -> list[str]:
    """Verify CLI entry points (files with argparse + __main__) are in cli-tools.md."""
    issues = []
    cli_content = _read_ref_doc("cli-tools.md")
    if not cli_content:
        issues.append("coverage: docs/reference/cli-tools.md not found")
        return issues

    # Scan for Python files that are CLI entry points
    for tool_dir in list(KNOWN_TOOLS) + ["docs"]:
        tool_path = REPO_ROOT / tool_dir
        if not tool_path.is_dir():
            continue
        for py_file in sorted(tool_path.glob("*.py")):
            if py_file.name.startswith("test_") or py_file.name == "__init__.py":
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
            except OSError:
                continue
            # Heuristic: file has argparse and __main__ guard = CLI entry point
            if "argparse" in content and '__name__' in content and '__main__' in content:
                if py_file.stem not in cli_content:
                    issues.append(f"coverage: CLI tool {tool_dir}/{py_file.name} not in cli-tools.md")

    return issues


def check_index_completeness(docs: list[Path]) -> list[str]:
    """Verify every doc file has an entry in _index.md."""
    issues = []
    if not INDEX_FILE.exists():
        issues.append("coverage: docs/_index.md not found")
        return issues

    index_content = INDEX_FILE.read_text(encoding="utf-8").lower()
    for doc in docs:
        rel = doc.relative_to(DOCS_DIR).as_posix()
        # Check that the relative path appears in the index
        if rel.lower() not in index_content:
            issues.append(f"index: {rel} not listed in _index.md")

    return issues


def main():
    parser = argparse.ArgumentParser(description="Documentation framework conformance checker")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero on any issue (warnings included)")
    args = parser.parse_args()

    framework_version = get_framework_version()
    docs = find_docs()
    all_errors = []
    all_warnings = []

    if not docs:
        print(f"Framework v{framework_version} — no docs to check")
        return

    for doc in docs:
        errors, warnings = check_doc(doc, framework_version)
        all_errors.extend(errors)
        all_warnings.extend(warnings)

    # Coverage and index checks are warnings (gaps, not broken docs)
    all_warnings.extend(check_coverage(docs))
    all_warnings.extend(check_hooks_coverage())
    all_warnings.extend(check_commands_coverage())
    all_warnings.extend(check_cli_coverage())
    all_warnings.extend(check_index_completeness(docs))

    if all_errors or all_warnings:
        total = len(all_errors) + len(all_warnings)
        print(f"Framework v{framework_version} — {total} issue(s):\n")
        for issue in all_errors:
            print(f"  ERROR   {issue}")
        for issue in all_warnings:
            print(f"  WARN    {issue}")

        if all_errors:
            sys.exit(1)
        if args.strict and all_warnings:
            sys.exit(1)
    else:
        print(f"Framework v{framework_version} — {len(docs)} doc(s), all conformant")


if __name__ == "__main__":
    main()
