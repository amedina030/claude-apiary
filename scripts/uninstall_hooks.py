#!/usr/bin/env python3
"""Uninstall apiary's hook entries from ~/.claude/settings.json (todo #261).

Mirror of ``setup.py --global`` for the hook-registration half of install.
Removes every apiary-owned entry (identified via
``core.hooks_lib.is_apiary_entry``) and leaves unrelated entries alone.
The context-rules managed zone in ``~/.claude/CLAUDE.md`` is NOT touched
— run ``scripts/install_context_rules.py --uninstall`` for that side of
the uninstall story.

Usage::

    python scripts/uninstall_hooks.py --list        # report without changing anything
    python scripts/uninstall_hooks.py --dry-run     # alias for --list
    python scripts/uninstall_hooks.py --uninstall   # actually remove
    python scripts/uninstall_hooks.py --uninstall --settings /path/to/settings.json

By default the target is ``~/.claude/settings.json``. Pass ``--settings``
to target a project-local settings file instead.

Exit codes::
    0  success (or nothing to do)
    1  error reading / writing settings
    2  bad arguments
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.hooks_lib import is_apiary_entry, remove_hooks  # noqa: E402

DEFAULT_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def _list_report(settings_path: Path) -> Dict[str, Any]:
    """Return a dict describing which apiary entries are currently installed.

    Unlike ``remove_hooks``, this is pure read-only and does not touch
    the file. Used by ``--list`` and ``--dry-run``.
    """
    report: Dict[str, Any] = {
        "settings_path": str(settings_path),
        "existed": settings_path.exists(),
        "owned": [],
        "total_counts": {},
    }
    if not report["existed"]:
        return report
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report["error"] = f"{settings_path}: {exc}"
        return report
    if not isinstance(settings, dict):
        return report
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return report
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        report["total_counts"][event] = len(entries)
        for entry in entries:
            if is_apiary_entry(entry):
                report["owned"].append({"event": event, "entry": entry})
    return report


def _print_list(report: Dict[str, Any]) -> None:
    print(f"settings file: {report['settings_path']}")
    if not report["existed"]:
        print("  (file does not exist — nothing installed)")
        return
    if "error" in report:
        print(f"  error: {report['error']}")
        return
    if not report["owned"]:
        print("  no apiary-owned hook entries found")
        return
    print(f"  apiary-owned entries: {len(report['owned'])}")
    # Group by event for readability
    by_event: Dict[str, list] = {}
    for item in report["owned"]:
        by_event.setdefault(item["event"], []).append(item["entry"])
    for event, entries in sorted(by_event.items()):
        total = report["total_counts"].get(event, len(entries))
        print(f"  {event} ({len(entries)}/{total} owned):")
        for entry in entries:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                matcher = entry.get("matcher", "")
                label = f"matcher={matcher!r} " if matcher else ""
                print(f"    - {label}{cmd}")


def _print_uninstall(report: Dict[str, Any]) -> None:
    dry = " (DRY RUN)" if report["dry_run"] else ""
    print(f"uninstall{dry}: {report['settings_path']}")
    if not report["existed"]:
        print("  (file does not exist — nothing to do)")
        return
    if not report["removed"]:
        print("  no apiary-owned hook entries to remove")
        return
    print(f"  removed: {len(report['removed'])} entries")
    by_event: Dict[str, int] = {}
    for item in report["removed"]:
        by_event[item["event"]] = by_event.get(item["event"], 0) + 1
    for event, count in sorted(by_event.items()):
        remaining = report["remaining_counts"].get(event, 0)
        print(f"    {event}: {count} removed, {remaining} remaining")
    if report["dry_run"]:
        print("\n  Dry run complete. Re-run with --uninstall to actually remove.")
    else:
        print("\n  Settings file updated.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list",
        dest="list_mode",
        action="store_true",
        help="List apiary-owned hook entries without changing anything.",
    )
    group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Report what would be removed without changing anything.",
    )
    group.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove apiary-owned hook entries from the settings file.",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        help=f"Settings file to operate on (default: {DEFAULT_SETTINGS_PATH})",
    )
    args = parser.parse_args()

    settings_path = args.settings.expanduser()

    if args.list_mode:
        report = _list_report(settings_path)
        _print_list(report)
        return 1 if "error" in report else 0

    if args.dry_run:
        report = remove_hooks(settings_path, dry_run=True)
        _print_uninstall(report)
        return 0

    if args.uninstall:
        try:
            report = remove_hooks(settings_path, dry_run=False)
        except OSError as exc:
            print(f"error writing {settings_path}: {exc}", file=sys.stderr)
            return 1
        _print_uninstall(report)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
