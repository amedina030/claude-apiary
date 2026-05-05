#!/usr/bin/env python3
"""Apiary target registry CLI — inspect and verify the .repos/ index.

Reads ``<apiary>/.repos/registry.json`` (built by the resolver in
``core/utils/state.py``) and exposes two subcommands::

    targets.py list             # tabular print of every registered target
    targets.py verify           # check each real_path still exists; flip
                                # verified_ok in the registry, report failures

Used as ``apiary targets list`` etc. via the launcher.

Spec: scribe note C-2026-46.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.utils.filelock import FileLock
from core.utils import state


def _load(apiary_repo: Path) -> dict:
    """Read the registry. Returns ``{}`` for missing/malformed file —
    matches the resolver's tolerance so list/verify on a fresh checkout
    print "no targets" instead of crashing."""
    return state._load_registry(apiary_repo)


def _save(apiary_repo: Path, data: dict) -> None:
    state._save_registry(apiary_repo, data)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_row(id_str: str, entry: dict) -> str:
    name = entry.get("name", "?")
    real_path = entry.get("real_path", "?")
    last_used = entry.get("last_used", "?")
    verified = "ok" if entry.get("verified_ok") else "MISSING"
    return f"  {id_str:>3}  {name:<30}  {verified:<7}  last={last_used}  {real_path}"


def cmd_list(args: argparse.Namespace) -> int:
    apiary = state.resolve_apiary_repo()
    registry = _load(apiary)
    if not registry:
        print("No registered targets.")
        return 0
    print(f"Registered targets (apiary repo: {apiary}):")
    print(f"  {'id':>3}  {'name':<30}  {'status':<7}  last_used / real_path")
    for id_str in sorted(registry.keys(), key=lambda s: int(s) if s.isdigit() else 0):
        entry = registry.get(id_str)
        if not isinstance(entry, dict):
            continue
        print(_fmt_row(id_str, entry))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Walk every registered target, set ``verified_ok`` to whether the
    real_path still exists as a directory. Reports failures to stdout but
    leaves all entries in place — pruning is a separate (future)
    operation. Always exits 0 unless registry IO fails; the verification
    *result* is reported in stdout, the *command* itself succeeded."""
    apiary = state.resolve_apiary_repo()
    failures: list[tuple[str, dict]] = []
    with FileLock(state.registry_path(apiary)):
        registry = _load(apiary)
        if not registry:
            print("No registered targets.")
            return 0
        for id_str, entry in registry.items():
            if not isinstance(entry, dict):
                continue
            real_path = entry.get("real_path", "")
            ok = bool(real_path) and Path(real_path).is_dir()
            entry["verified_ok"] = ok
            entry["last_verified"] = _now_iso()
            if not ok:
                failures.append((id_str, entry))
        _save(apiary, registry)
    print(f"Verified {len(registry)} target(s).")
    if failures:
        print(f"  {len(failures)} missing — review and decide whether to repair or remove:")
        for id_str, entry in failures:
            print(f"    {id_str}: {entry.get('name', '?')} -> {entry.get('real_path', '?')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apiary target registry inspector")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List all registered targets")
    sub.add_parser("verify", help="Check each target's path exists; update verified_ok")

    args = parser.parse_args(argv)

    handlers = {
        "list": cmd_list,
        "verify": cmd_verify,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
