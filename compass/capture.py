#!/usr/bin/env python3
"""Compass observation capture CLI — the write side of ``/wrapup`` Step 4.

``core/commands/wrapup.md`` used to spell out the observation schema, the
target path, the dimension list and a two-nested-``$()`` validate command in
prose, then have the model hand-write the JSON file itself. That is
orchestration, and it belongs in code (review X-8, plan row 3.8).

The skill now writes the observation JSON to a scratch file and calls::

    capture.py dimensions            # what to look for, and which are volatile
    capture.py store --content-file OBS.json --session-id abc12345

``store`` validates against ``compass/store.validate_observation`` (including
the session-id-matches-filename guard) and only then writes to
``<state-dir>/compass/observations/<sid>.json``. Nothing is written when
validation fails, so a bad payload can never poison the profile.

Capture is non-blocking by contract: every failure exits non-zero with a
one-line reason on stderr and the skill is told to warn and move on.

Exit codes: ``0`` stored/valid, ``1`` invalid payload or write failure,
``2`` usage error.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compass import store  # noqa: E402


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _read_payload(content_file: str) -> tuple[dict | None, str | None]:
    path = Path(content_file)
    if not path.is_file():
        return None, f"observation file not found: {path}"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    if not raw:
        return None, f"observation file is empty: {path}"
    # Agents wrap JSON in fences even when told not to; tolerate one wrapper.
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            raw = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON in {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"expected a JSON object in {path}, got {type(data).__name__}"
    return data, None


def _short(session_id: str) -> str:
    return session_id.split("-", 1)[0][:8].lower()


def _validate(data: dict, session_id: str | None) -> list[str]:
    return store.validate_observation(data, expected_session_id=session_id)


def cmd_dimensions(args: argparse.Namespace) -> int:
    config = store.load_dimensions()
    if args.json:
        print(json.dumps(config, indent=2))
        return 0
    for dim in config["dimensions"]:
        marker = "volatile" if dim.get("volatile") else "stable"
        print(f"{dim['name']} [{marker}] — {dim.get('description', '').strip()}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    data, error = _read_payload(args.content_file)
    if error:
        return _fail(error)
    errors = _validate(data, args.session_id)
    if errors:
        print("observation payload is invalid:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(f"ok — {len(data.get('observations', []))} observation(s)")
    return 0


def cmd_store(args: argparse.Namespace) -> int:
    data, error = _read_payload(args.content_file)
    if error:
        return _fail(error)

    session_id = args.session_id or data.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return _fail("no --session-id given and the payload has no usable session_id")

    errors = _validate(data, session_id)
    if errors:
        print("observation payload is invalid — nothing was written:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    observations = data.get("observations", [])
    if not observations and not args.allow_empty:
        # An honest empty capture is a valid outcome, but writing an empty file
        # adds a row to every future synthesis for no signal. Skip by default.
        print(f"no observations to store for session {_short(session_id)} — "
              "skipped (pass --allow-empty to write the file anyway)")
        return 0

    if args.dry_run:
        print(json.dumps({
            "stored": False,
            "dry_run": True,
            "path": str(store.observation_path(session_id)),
            "observations": len(observations),
        }, indent=2))
        return 0

    try:
        store.ensure_layout()
        target = store.observation_path(session_id)
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return _fail(f"could not write the observation file: {exc}")

    print(json.dumps({
        "stored": True,
        "path": str(target),
        "session_id": _short(session_id),
        "observations": len(observations),
        "volatile": sum(1 for o in observations if o.get("volatility") == "volatile"),
    }, indent=2))
    return 0


def cmd_template(args: argparse.Namespace) -> int:
    """Print a skeleton payload so the skill never retypes the schema."""
    session_id = _short(args.session_id) if args.session_id else "<8-char prefix>"
    captured = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(json.dumps({
        "session_id": session_id,
        "captured_at": captured,
        "observations": [{
            "dimension": store.dimension_names()[0],
            "observation": "<1-2 sentences describing the trait/pattern>",
            "evidence": "<short quote or paraphrase from this session>",
            "volatility": "stable",
        }],
    }, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and store a /wrapup compass observation payload")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dims = sub.add_parser("dimensions", help="Print the dimensions to look for")
    p_dims.add_argument("--json", action="store_true", help="Print the raw config")
    p_dims.set_defaults(func=cmd_dimensions)

    p_template = sub.add_parser("template", help="Print an empty observation payload")
    p_template.add_argument("--session-id")
    p_template.set_defaults(func=cmd_template)

    p_validate = sub.add_parser("validate", help="Validate a payload without storing it")
    p_validate.add_argument("--content-file", dest="content_file", required=True)
    p_validate.add_argument("--session-id")
    p_validate.set_defaults(func=cmd_validate)

    p_store = sub.add_parser("store", help="Validate then write the observation file")
    p_store.add_argument("--content-file", dest="content_file", required=True)
    p_store.add_argument("--session-id")
    p_store.add_argument("--allow-empty", action="store_true",
                         help="Write the file even when observations is empty")
    p_store.add_argument("--dry-run", action="store_true",
                         help="Validate and report the target path, write nothing")
    p_store.set_defaults(func=cmd_store)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
