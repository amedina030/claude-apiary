#!/usr/bin/env python3
"""Apiary bootstrap CLI — apply a profile to a target repo's .claude/settings.json.

Source of truth: ``core/apiary_bootstrap.py`` in the apiary repo. Installed
to ``~/.claude/apiary_bootstrap.py`` by ``setup.py --global``.

Usage::

    python ~/.claude/apiary_bootstrap.py --profile unreal [--target PATH] [--force]

Resolves ``<profile>`` via ``<apiary-repo>/profiles/<name>.jsonc`` (walking
``extends`` chains and deep-merging), loads the target's existing
``.claude/settings.json`` (if any), merges apiary-owned top-level keys
preserving non-apiary keys, writes ``.claude/settings.json`` and
``.apiary/bootstrap_state.json``. Re-runs show a diff and prompt before
overwriting unless ``--force``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _prepare_sys_path() -> None:
    """Put the apiary repo root on sys.path so ``core.*`` imports resolve.

    Works from two invocation contexts:

    - Running from the repo (``<apiary>/core/apiary_bootstrap.py``) —
      ``__file__.parent.parent`` is the apiary repo root.
    - Running from the installed copy (``~/.claude/apiary_bootstrap.py``) —
      ``__file__.parent.parent`` is ``~``, which doesn't have ``core/``.
      Fall back to the ``~/.claude/apiary.json`` pointer file to locate
      the real repo.
    """
    here = Path(__file__).resolve()
    candidate = here.parent.parent
    if (candidate / "core" / "apiary_profiles.py").is_file():
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        return
    pointer = Path.home() / ".claude" / "apiary.json"
    if not pointer.is_file():
        print("error: cannot locate apiary repo (pointer file missing)", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"error: apiary pointer file {pointer} malformed", file=sys.stderr)
        sys.exit(1)
    repo_path = data.get("repo_path", "") if isinstance(data, dict) else ""
    if not repo_path or not Path(repo_path).is_dir():
        print(f"error: apiary pointer repo_path missing or invalid: {repo_path!r}", file=sys.stderr)
        sys.exit(1)
    sys.path.insert(0, str(Path(repo_path)))


_prepare_sys_path()

from core.apiary_profiles import (
    ProfileCycleError,
    ProfileError,
    ProfileNotFoundError,
    ProfileSchemaError,
    ResolvedProfile,
    list_available,
    resolve,
)
from core.utils.apiary_pointer import get_repo_path
from core.utils.jsonc import JsoncParseError

STATE_SCHEMA_VERSION = 1
_STATE_FILENAME = "bootstrap_state.json"
_SETTINGS_PATH = ".claude/settings.json"
_PROFILES_SUBDIR = "profiles"


class BootstrapError(Exception):
    """Raised for user-facing bootstrap failures (bad arguments, drift abort)."""


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return _run(args)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ProfileCycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ProfileSchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except JsoncParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="apiary_bootstrap.py",
        description="Bootstrap a target repo's .claude/settings.json from an apiary profile.",
    )
    p.add_argument("--profile", required=True, help="profile name under <apiary>/profiles/")
    p.add_argument("--target", type=Path, default=None, help="target repo root (default: cwd)")
    p.add_argument("--force", action="store_true", help="skip drift prompt on re-run")
    p.add_argument(
        "--apiary-repo",
        type=Path,
        default=None,
        help="override apiary repo root (default: via ~/.claude/apiary.json pointer)",
    )
    return p.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    target_repo = (args.target or Path.cwd()).resolve()
    apiary_repo = _resolve_apiary_repo(args.apiary_repo)
    profiles_dir = apiary_repo / _PROFILES_SUBDIR
    if not profiles_dir.is_dir():
        raise BootstrapError(f"apiary profiles dir not found: {profiles_dir}")

    resolved = resolve(args.profile, profiles_dir)

    existing_settings = _load_existing_settings(target_repo)
    merged_settings, owned_keys = _merge_profile_into_settings(existing_settings, resolved.merged)

    state_path = target_repo / ".apiary" / _STATE_FILENAME
    prior_state = _read_state(state_path)

    if prior_state is not None and existing_settings != merged_settings:
        if not args.force:
            _print_diff(existing_settings, merged_settings, owned_keys)
            if not _prompt_yes_no(
                "Apply these changes to .claude/settings.json? [y/N] "
            ):
                raise BootstrapError("aborted by user")

    _write_settings(target_repo, merged_settings)
    _write_state(state_path, resolved, owned_keys)

    _print_summary(target_repo, resolved, owned_keys, merged_settings)
    return 0


def _resolve_apiary_repo(override: Path | None) -> Path:
    if override is not None:
        if not override.is_dir():
            raise BootstrapError(f"--apiary-repo {override} is not a directory")
        return override.resolve()
    candidate = get_repo_path()
    if candidate is None:
        raise BootstrapError(
            "cannot locate apiary repo. Run apiary's setup.py --global first, "
            "or pass --apiary-repo explicitly."
        )
    return candidate


def _load_existing_settings(target_repo: Path) -> dict:
    path = target_repo / _SETTINGS_PATH
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{path}: not valid JSON ({exc.msg} at line {exc.lineno})")


def _merge_profile_into_settings(
    existing: dict, profile_merged: dict
) -> tuple[dict, list[str]]:
    """Return (new_settings, apiary_owned_top_level_keys).

    Apiary-owned = any top-level key present in the resolved profile. The
    profile's value fully replaces whatever was previously at that key,
    keeping the write idempotent (re-runs without profile changes are a
    no-op). Top-level keys the profile doesn't mention pass through
    verbatim. Users who want to add permissions / hooks alongside an
    apiary-managed config should extend via a custom profile or a
    settings.local.json layer, not by hand-editing the managed file.
    """
    owned_keys = sorted(profile_merged.keys())
    out = dict(existing)
    for key in owned_keys:
        out[key] = profile_merged[key]
    return out, owned_keys


def _read_state(state_path: Path) -> dict | None:
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_settings(target_repo: Path, merged: dict) -> None:
    path = target_repo / _SETTINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_lf(path, json.dumps(merged, indent=2) + "\n")


def _write_state(state_path: Path, resolved: ResolvedProfile, owned_keys: list[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "profile": resolved.name,
        "profiles_applied": resolved.profiles_applied,
        "profile_content_hashes": resolved.content_hashes,
        "applied_apiary_keys": owned_keys,
        "last_bootstrap_ts": datetime.now(timezone.utc).isoformat(),
    }
    _write_lf(state_path, json.dumps(payload, indent=2) + "\n")


def _write_lf(path: Path, text: str) -> None:
    """Write LF-terminated text regardless of platform so the managed file
    stays byte-identical across Windows/macOS/Linux re-runs."""
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _print_diff(existing: dict, new: dict, owned_keys: list[str]) -> None:
    print("\nChanges to apiary-owned keys in .claude/settings.json:\n", file=sys.stderr)
    for key in owned_keys:
        before = existing.get(key)
        after = new.get(key)
        if before == after:
            continue
        print(f"  {key}:", file=sys.stderr)
        print(f"    before: {_oneline(before)}", file=sys.stderr)
        print(f"    after:  {_oneline(after)}", file=sys.stderr)
    print("", file=sys.stderr)


def _oneline(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _prompt_yes_no(prompt: str) -> bool:
    if not sys.stdin.isatty():
        raise BootstrapError(
            "re-run would overwrite existing settings and stdin is not a TTY. "
            "Pass --force to apply non-interactively."
        )
    answer = input(prompt).strip().lower()
    return answer in ("y", "yes")


def _print_summary(
    target_repo: Path,
    resolved: ResolvedProfile,
    owned_keys: list[str],
    merged: dict,
) -> None:
    settings_path = target_repo / _SETTINGS_PATH
    state_path = target_repo / ".apiary" / _STATE_FILENAME
    print(f"Applied profile chain: {' → '.join(resolved.profiles_applied)}")
    print(f"Wrote {settings_path}")
    print(f"Wrote {state_path}")
    print(f"Apiary-owned top-level keys: {', '.join(owned_keys) if owned_keys else '(none)'}")
    for key in owned_keys:
        print(f"  {key}: {_count_summary(merged.get(key))}")


def _count_summary(value: Any) -> str:
    if isinstance(value, dict):
        return f"{len(value)} sub-key(s): {', '.join(sorted(value.keys()))}"
    if isinstance(value, list):
        return f"{len(value)} item(s)"
    return type(value).__name__


if __name__ == "__main__":
    sys.exit(main())
