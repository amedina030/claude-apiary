#!/usr/bin/env python3
"""Install / sync / check apiary context-rules in ~/.claude/CLAUDE.md (#224).

The installer fully owns a managed zone in ~/.claude/CLAUDE.md, marked by
outer sentinels:

    <!-- apiary-context-rules-start -->
    <!-- apiary-context-rule:<id> hash=<sha256> -->
    <body>
    <!-- /apiary-context-rule:<id> -->
    <!-- apiary-context-rules-end -->

Content outside the zone is never touched. Content inside must match what
the installer wrote — manual edits are detected as tampering and require
--force to overwrite.

Usage:
    python scripts/install_context_rules.py                   # interactive
    python scripts/install_context_rules.py --list
    python scripts/install_context_rules.py --install <id>...
    python scripts/install_context_rules.py --install-all
    python scripts/install_context_rules.py --install-category <cat>
    python scripts/install_context_rules.py --uninstall <id>...
    python scripts/install_context_rules.py --sync
    python scripts/install_context_rules.py --check
    python scripts/install_context_rules.py --diff <id>
    python scripts/install_context_rules.py --dry-run --install-all
    python scripts/install_context_rules.py --force --install-all
    python scripts/install_context_rules.py --replace-stopgap --install-all
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import context_rules as cr  # noqa: E402

DEFAULT_TARGET = Path.home() / ".claude" / "CLAUDE.md"

# Substrings used by --replace-stopgap to detect inline rule bodies that
# pre-date the installer. Each entry is a tuple of (rule_id, marker substr).
# Markers must be distinctive phrases that appear in the stopgap inline
# content AND in the source rule body (since the stopgap is a copy of the
# rule). _strip_stopgap_paragraphs() restricts matching to text OUTSIDE
# any existing managed zone so it never strips zone contents.
STOPGAP_MARKERS = (
    ("recover_from_trivial_errors", "fix it and retry in the same turn"),
    ("keep_chaining_mid_plan", "Over-chunking successful work"),
    ("no_coauthored_by", "Co-Authored-By: Claude"),
)

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_TAMPER = 2
EXIT_USAGE = 64


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------


def _read_target(target: Path) -> str:
    if not target.exists():
        return ""
    return target.read_text(encoding="utf-8")


def _write_target(target: Path, text: str, dry_run: bool) -> None:
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _status_for_rule(rule: cr.Rule, installed: dict[str, cr.InstalledRule]) -> str:
    ir = installed.get(rule.id)
    if ir is None:
        return "not installed"
    if ir.body_hash != ir.stored_hash:
        return "TAMPERED"
    if ir.stored_hash != rule.body_hash:
        return "out of date"
    return "installed"


def _zone_or_empty(target: Path) -> tuple[str, dict[str, cr.InstalledRule], bool]:
    """Return (text, {id: InstalledRule}, tamper_seen)."""
    text = _read_target(target)
    try:
        zone = cr.find_managed_zone(text)
    except cr.ZoneTamperError:
        return text, {}, True
    if zone is None:
        return text, {}, False
    return text, {ir.id: ir for ir in zone.rules}, bool(zone.stray_text)


# ---------------------------------------------------------------------------
# Stopgap removal
# ---------------------------------------------------------------------------


# Splits a markdown document into chunks at every level-2-or-deeper header
# line, keeping the header attached to its following body. Level-1 (`# `)
# headers are treated as document titles, not section boundaries — this
# matches the convention used in this project's CLAUDE.md files (single
# top-level title + ## / ### subsections) and lets paragraphs that sit
# directly under the title fall through to paragraph-level stripping.
_HEADER_SPLIT_RE = re.compile(r"(?m)^(?=#{2,6}[ \t])")


def _strip_paragraphs_in(text: str) -> str:
    """Drop blank-line-separated paragraphs containing any STOPGAP_MARKERS phrase.

    This is the fallback used for text that has no preceding markdown
    header — e.g. stopgap paragraphs inserted at the very top of CLAUDE.md
    above any section heading.
    """
    if not text:
        return text
    paragraphs = text.split("\n\n")
    keep = [p for p in paragraphs if not any(m in p for _, m in STOPGAP_MARKERS)]
    return "\n\n".join(keep)


def _strip_sections_in(text: str) -> str:
    """Drop entire markdown sections containing any STOPGAP_MARKERS phrase.

    A "section" is a markdown header line plus all following lines up to
    the next header (any level). Whole sections are removed when any
    marker substring appears anywhere in the section body — this handles
    multi-paragraph rule bodies where only one paragraph contains the
    distinctive phrase but every adjacent paragraph belongs to the same
    rule and must go with it (#230).

    Content before the first header has no enclosing section, so it falls
    back to paragraph-level stripping.
    """
    if not text:
        return text
    chunks = _HEADER_SPLIT_RE.split(text)
    if not chunks:
        return text
    out: list[str] = []
    # Pre-header preamble: paragraph-level fallback.
    preamble = chunks[0]
    if preamble:
        out.append(_strip_paragraphs_in(preamble))
    # Each remaining chunk starts with a header line.
    for chunk in chunks[1:]:
        if any(m in chunk for _, m in STOPGAP_MARKERS):
            continue
        out.append(chunk)
    return "".join(out)


def _strip_stopgap_paragraphs(text: str) -> str:
    """Remove stopgap content from `text`, ONLY in regions outside any
    existing managed zone.

    The stopgap markers are distinctive phrases that appear in both the
    inline stopgap copy AND in the source rule body inside the managed
    zone. Restricting the strip to outside-zone regions ensures the
    installer never accidentally chops out a real rule body.

    Section-level removal is used so that multi-paragraph rule bodies are
    dropped atomically — earlier paragraph-level stripping (#230) left
    orphan paragraphs from the same rule when only one paragraph carried
    the marker substring.
    """
    try:
        zone = cr.find_managed_zone(text)
    except cr.ZoneTamperError:
        zone = None

    if zone is None:
        return _strip_sections_in(text)

    prefix = text[: zone.start]
    middle = text[zone.start : zone.end]  # untouched
    suffix = text[zone.end :]
    return _strip_sections_in(prefix) + middle + _strip_sections_in(suffix)


# ---------------------------------------------------------------------------
# Rebuild zone
# ---------------------------------------------------------------------------


def _splice_zone(text: str, new_zone: str) -> str:
    """Replace existing zone in `text` with `new_zone`, or append if absent."""
    try:
        existing = cr.find_managed_zone(text)
    except cr.ZoneTamperError:
        # Caller is responsible for refusing on tamper unless --force.
        existing = None

    if existing is None:
        if not text:
            return new_zone
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + sep + new_zone

    return text[: existing.start] + new_zone.rstrip("\n") + text[existing.end :]


def _select_rules(
    all_rules: list[cr.Rule],
    installed_ids: set[str],
    *,
    install_ids: set[str],
    uninstall_ids: set[str],
) -> list[cr.Rule]:
    """Compute the new set of installed rules after applying install/uninstall sets.

    Starts from the currently-installed set, adds install_ids (must exist on
    disk), removes uninstall_ids. Returns rules in source order.
    """
    by_id = {r.id: r for r in all_rules}
    target_ids = set(installed_ids)
    target_ids -= uninstall_ids
    target_ids |= install_ids
    selected = [by_id[i] for i in target_ids if i in by_id]
    selected.sort(key=lambda r: (r.category, r.id))
    return selected


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_list(args, all_rules, target):
    text, installed, tamper = _zone_or_empty(target)
    if tamper:
        print("WARNING: managed zone is tampered or malformed; --force to rebuild")
    width = max((len(r.id) for r in all_rules), default=10)
    for r in all_rules:
        status = _status_for_rule(r, installed)
        print(f"  {r.id.ljust(width)}  [{r.category}]  {status}")
        if args.list:
            print(f"    {r.title}")
    return EXIT_OK


def cmd_check(args, all_rules, target):
    text = _read_target(target)
    report = cr.compute_drift(text, all_rules)
    # For --check, "not_installed" is informational only. The user picks
    # what to install; not installing isn't drift. Strip it from drift count.
    drift_count = (
        len(report.missing_source)
        + len(report.hash_mismatch)
        + len(report.tampered_bodies)
        + (1 if report.stray_text else 0)
    )
    if drift_count == 0:
        installed_ct = sum(1 for r in all_rules if any(True for _ in [r]) and report.zone_present)
        print(f"context-rules: clean ({len(all_rules)} source rule(s) available)")
        return EXIT_OK
    print(f"context-rules: drift detected ({drift_count} issue(s))")
    if report.tampered_bodies:
        print(f"  TAMPERED: {', '.join(report.tampered_bodies)}")
    if report.hash_mismatch:
        print(f"  out of date: {', '.join(report.hash_mismatch)}")
    if report.missing_source:
        print(f"  installed but no source: {', '.join(report.missing_source)}")
    if report.stray_text:
        print("  stray content inside managed zone")
    if report.not_installed:
        print(f"  (not installed: {', '.join(report.not_installed)})")
    return EXIT_TAMPER if (report.tampered_bodies or report.stray_text) else EXIT_DRIFT


def cmd_diff(args, all_rules, target):
    rule_id = args.diff
    by_id = {r.id: r for r in all_rules}
    if rule_id not in by_id:
        print(f"unknown rule: {rule_id}", file=sys.stderr)
        return EXIT_USAGE
    text, installed, _ = _zone_or_empty(target)
    if rule_id not in installed:
        print(f"{rule_id}: not installed")
        return EXIT_OK
    src_body = by_id[rule_id].body
    inst_body = installed[rule_id].body
    diff = difflib.unified_diff(
        inst_body.splitlines(keepends=True),
        src_body.splitlines(keepends=True),
        fromfile=f"installed:{rule_id}",
        tofile=f"source:{rule_id}",
    )
    sys.stdout.writelines(diff)
    return EXIT_OK


def cmd_install_or_sync(args, all_rules, target, *, install_ids, uninstall_ids, sync):
    text = _read_target(target)

    if args.replace_stopgap:
        text = _strip_stopgap_paragraphs(text)

    try:
        existing = cr.find_managed_zone(text)
    except cr.ZoneTamperError as e:
        if not args.force:
            print(
                f"ERROR: managed zone is tampered or malformed: {e}\n"
                f"  Restore ~/.claude/CLAUDE.md or re-run with --force.",
                file=sys.stderr,
            )
            return EXIT_TAMPER
        existing = None

    # Tamper check at the rule level
    installed_ids: set[str] = set()
    if existing is not None:
        if existing.stray_text and not args.force:
            print(
                "ERROR: managed zone contains stray content not enclosed in rule sentinels.\n"
                "  Restore ~/.claude/CLAUDE.md or re-run with --force.",
                file=sys.stderr,
            )
            return EXIT_TAMPER
        for ir in existing.rules:
            if ir.body_hash != ir.stored_hash and not args.force:
                print(
                    f"ERROR: rule '{ir.id}' has been hand-edited inside the managed zone.\n"
                    f"  Restore ~/.claude/CLAUDE.md or re-run with --force.",
                    file=sys.stderr,
                )
                return EXIT_TAMPER
            installed_ids.add(ir.id)

    # Validate install_ids exist
    by_id = {r.id: r for r in all_rules}
    unknown = [i for i in install_ids if i not in by_id]
    if unknown:
        print(f"unknown rule id(s): {', '.join(unknown)}", file=sys.stderr)
        return EXIT_USAGE

    if sync:
        # Sync = re-render whatever is currently installed.
        target_rules = [by_id[i] for i in installed_ids if i in by_id]
        target_rules.sort(key=lambda r: (r.category, r.id))
    else:
        target_rules = _select_rules(
            all_rules,
            installed_ids,
            install_ids=install_ids,
            uninstall_ids=uninstall_ids,
        )

    if not target_rules:
        # Empty target → remove zone entirely if present
        if existing is None:
            print("nothing to install (no managed zone present)")
            return EXIT_OK
        new_text = text[: existing.start].rstrip("\n") + "\n" + text[existing.end :].lstrip("\n")
        if args.dry_run:
            print(f"would remove managed zone from {target}")
            return EXIT_OK
        _write_target(target, new_text, dry_run=False)
        print(f"removed managed zone from {target}")
        return EXIT_OK

    new_zone = cr.render_managed_zone(target_rules)
    new_text = _splice_zone(text, new_zone)

    if args.dry_run:
        action = "sync" if sync else "install"
        print(f"would {action} {len(target_rules)} rule(s) into {target}")
        for r in target_rules:
            print(f"  + {r.id}")
        return EXIT_OK

    _write_target(target, new_text, dry_run=False)
    action = "synced" if sync else "installed"
    print(f"{action} {len(target_rules)} rule(s) into {target}")
    for r in target_rules:
        print(f"  + {r.id}")
    return EXIT_OK


def cmd_interactive(args, all_rules, target):
    """Interactive prompt: per-rule y/n/view."""
    text, installed, tamper = _zone_or_empty(target)
    if tamper and not args.force:
        print(
            "ERROR: managed zone is tampered. Re-run with --force to rebuild.",
            file=sys.stderr,
        )
        return EXIT_TAMPER

    chosen: set[str] = set()
    for r in all_rules:
        status = _status_for_rule(r, installed)
        prompt = f"  {r.id} [{r.category}] ({status}) — install? [y/N/v]: "
        while True:
            answer = input(prompt).strip().lower()
            if answer in ("", "n"):
                break
            if answer == "v":
                print()
                print(r.body)
                continue
            if answer == "y":
                chosen.add(r.id)
                break
            print("  please answer y, n, or v")

    if not chosen:
        print("no rules selected")
        return EXIT_OK

    return cmd_install_or_sync(
        args,
        all_rules,
        target,
        install_ids=chosen,
        uninstall_ids=set(),
        sync=False,
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="CLAUDE.md path (default: ~/.claude/CLAUDE.md)")
    p.add_argument("--rules-dir", type=Path, default=cr.RULES_DIR, help="Source rules directory")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Bypass tamper checks and rebuild the zone")
    p.add_argument("--replace-stopgap", action="store_true", help="Strip known stopgap inline content before injecting")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="List rules and statuses")
    mode.add_argument("--install", nargs="+", metavar="ID", help="Install specific rule(s)")
    mode.add_argument("--install-all", action="store_true", help="Install every rule")
    mode.add_argument("--install-category", metavar="CAT", help="Install all rules in a category")
    mode.add_argument("--uninstall", nargs="+", metavar="ID", help="Uninstall specific rule(s)")
    mode.add_argument("--sync", action="store_true", help="Re-render currently installed rules from source")
    mode.add_argument("--check", action="store_true", help="Audit only; non-zero exit on drift")
    mode.add_argument("--diff", metavar="ID", help="Show diff between installed and source for a rule")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    all_rules = cr.load_all_rules(args.rules_dir)
    if not all_rules:
        print(f"no rules found under {args.rules_dir}", file=sys.stderr)
        return EXIT_USAGE

    target = args.target

    if args.list:
        return cmd_list(args, all_rules, target)
    if args.check:
        return cmd_check(args, all_rules, target)
    if args.diff:
        return cmd_diff(args, all_rules, target)

    if args.install:
        return cmd_install_or_sync(
            args, all_rules, target,
            install_ids=set(args.install), uninstall_ids=set(), sync=False,
        )
    if args.install_all:
        return cmd_install_or_sync(
            args, all_rules, target,
            install_ids={r.id for r in all_rules}, uninstall_ids=set(), sync=False,
        )
    if args.install_category:
        ids = {r.id for r in all_rules if r.category == args.install_category}
        if not ids:
            print(f"no rules in category: {args.install_category}", file=sys.stderr)
            return EXIT_USAGE
        return cmd_install_or_sync(
            args, all_rules, target,
            install_ids=ids, uninstall_ids=set(), sync=False,
        )
    if args.uninstall:
        return cmd_install_or_sync(
            args, all_rules, target,
            install_ids=set(), uninstall_ids=set(args.uninstall), sync=False,
        )
    if args.sync:
        return cmd_install_or_sync(
            args, all_rules, target,
            install_ids=set(), uninstall_ids=set(), sync=True,
        )

    # No flag → interactive mode
    return cmd_interactive(args, all_rules, target)


if __name__ == "__main__":
    sys.exit(main())
