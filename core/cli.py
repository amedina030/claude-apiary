#!/usr/bin/env python3
"""``apiary`` — unified CLI for the per-repo install model.

Subcommands dispatch to single-purpose modules so each piece can be
tested in isolation:

    apiary install --target <repo>     core/install.py
    apiary uninstall --target <repo>   core/uninstall.py
    apiary self-bootstrap              core/self_bootstrap.py
    apiary doctor [check] [--fix]      core/doctor.py
    apiary cascade-fix                 core/cascade.py
    apiary version                     prints <main-apiary>/VERSION

Run from inside a clone of main-apiary, or from a bootstrapped repo where
the per-repo launcher resolves main-apiary via the pointer file. Either
way the script needs to find main-apiary; pass ``--apiary-repo`` to override.

A single CLI replaces the legacy collection of standalone install
scripts; see ``docs/architecture/per-repo-install.md``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.utils import state


def _add_apiary_repo_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--apiary-repo", type=Path, default=None,
        help="Path to main-apiary (default: resolved via pointer / launcher env).",
    )


def _cmd_install(args: argparse.Namespace) -> int:
    from core import install as install_mod
    result = install_mod.install(
        args.target, profile=args.profile, apiary_repo=args.apiary_repo,
    )
    print(
        f"{'installed' if result.is_first_install else 're-applied'}: "
        f"uid={result.uid} name={result.name} slug={result.slug}\n"
        f"  state dir: {result.state_dir}\n"
        f"  apiary version: {result.apiary_version}"
    )
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from core import uninstall as uninstall_mod
    result = uninstall_mod.uninstall(
        args.target, apiary_repo=args.apiary_repo, remove_data=args.remove_data,
    )
    print(
        f"uninstalled: uid={result.uid} name={result.name}\n"
        f"  pin dir removed: {result.pin_dir_removed}\n"
        f"  hook entries removed: {result.hook_entries_removed}\n"
        f"  commands removed: {len(result.commands_removed)}\n"
        f"  CLAUDE.md zone removed: {result.claude_md_zone_removed}\n"
        f"  registry entry removed: {result.registry_entry_removed}\n"
        f"  state dir removed: {result.state_dir_removed}"
    )
    return 0


def _cmd_self_bootstrap(args: argparse.Namespace) -> int:
    from core import self_bootstrap as sb
    result = sb.self_bootstrap(args.apiary_repo)
    print(
        f"main-apiary bootstrapped at uid={result.uid} "
        f"({'fresh' if result.is_first_install else 'idempotent re-run'})"
    )
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from core import doctor
    forwarded: list[str] = []
    if args.subcommand:
        forwarded.append(args.subcommand)
    if args.fix:
        forwarded.append("--fix")
    if args.apiary_repo is not None:
        forwarded.extend(["--apiary-repo", str(args.apiary_repo)])
    return doctor.main(forwarded)


def _cmd_cascade(args: argparse.Namespace) -> int:
    from core import cascade as cascade_mod
    apiary = state.resolve_apiary_repo(args.apiary_repo).resolve()
    report = cascade_mod.cascade_fix(apiary)
    print(f"cascade-fix at {apiary}: updated {len(report.updated)} repo(s); "
          f"skipped {len(report.skipped)}")
    for uid in report.updated:
        print(f"  updated uid={uid}")
    for uid, reason in report.skipped:
        print(f"  skipped uid={uid}: {reason}")
    return 0


def _cmd_version(args: argparse.Namespace) -> int:
    apiary = state.resolve_apiary_repo(args.apiary_repo)
    print(state.read_apiary_version(apiary))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apiary",
        description="Per-repo apiary install / drift / consistency tooling.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="bootstrap apiary into a target repo")
    p_install.add_argument("--target", type=Path, required=True, help="target repo path")
    p_install.add_argument("--profile", default="base", help="apiary profile (default: base)")
    _add_apiary_repo_arg(p_install)
    p_install.set_defaults(func=_cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="remove apiary from a bootstrapped repo")
    p_uninstall.add_argument("--target", type=Path, required=True, help="target repo path")
    p_uninstall.add_argument(
        "--remove-data", action="store_true",
        help="also delete <main-apiary>/.repos/<slug>/ (per-target state)",
    )
    _add_apiary_repo_arg(p_uninstall)
    p_uninstall.set_defaults(func=_cmd_uninstall)

    p_sb = sub.add_parser("self-bootstrap", help="initialize main-apiary on a new machine")
    _add_apiary_repo_arg(p_sb)
    p_sb.set_defaults(func=_cmd_self_bootstrap)

    p_doctor = sub.add_parser("doctor", help="run consistency checks")
    from core import doctor as _doctor  # choices derived from the check registry
    p_doctor.add_argument(
        "subcommand", nargs="?",
        choices=tuple(_doctor.CHECKS),
        help="single check to run; omit to run all",
    )
    p_doctor.add_argument(
        "--fix", action="store_true",
        help=("apply safe fixes for the named check (supported: "
              f"{', '.join(_doctor.FIXES)}); requires a check name"),
    )
    _add_apiary_repo_arg(p_doctor)
    p_doctor.set_defaults(func=_cmd_doctor)

    p_cascade = sub.add_parser(
        "cascade-fix",
        help="rewrite every bootstrapped repo's main-apiary-pointer to the current location",
    )
    _add_apiary_repo_arg(p_cascade)
    p_cascade.set_defaults(func=_cmd_cascade)

    p_version = sub.add_parser("version", help="print main-apiary's pinned version")
    _add_apiary_repo_arg(p_version)
    p_version.set_defaults(func=_cmd_version)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
