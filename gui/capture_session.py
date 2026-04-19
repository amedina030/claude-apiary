"""`python -m gui.capture_session` — launch the GUI with pty capture enabled.

Usage:
    python -m gui.capture_session --label tool_permission
    python -m gui.capture_session list

The labeled form launches the GUI with capture on, writing to a `.bin` file
under `~/.claude/apiary_gui/captures/`. Reproduce the interactive UI you want
handled, then close the window — the file is the fixture.

`list` prints existing captures (oldest → newest).
"""

from __future__ import annotations

import argparse
import os
import sys

from gui import pty_capture


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="gui.capture_session")
    parser.add_argument(
        "--label",
        default=None,
        help="short label for the capture filename (e.g. 'tool_permission')",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="list existing captures")
    p_show = sub.add_parser("show", help="print a capture's tail, ansi-stripped")
    p_show.add_argument("path", help="path to a .bin capture file")
    p_show.add_argument("--tail", type=int, default=4000,
                        help="number of trailing chars to print (default 4000)")

    args = parser.parse_args(argv)

    if args.command == "list":
        captures = pty_capture.list_captures()
        if not captures:
            print(f"no captures in {pty_capture.CAPTURES_DIR}")
            return 0
        for p in captures:
            print(p)
        return 0

    if args.command == "show":
        from pathlib import Path as _P

        path = _P(args.path)
        data = path.read_bytes()
        text = data.decode("utf-8", errors="replace")
        stripped = pty_capture.strip_ansi(text)
        print(f"bytes: {len(data)}")
        print(f"--- tail ({args.tail} chars, ansi-stripped) ---")
        print(stripped[-args.tail:])
        return 0

    # Capture mode is opt-in via env var so the normal `python -m gui.app`
    # entry point stays completely untouched when capture isn't wanted.
    os.environ["APIARY_GUI_CAPTURE_LABEL"] = args.label or ""
    # Import after env var is set so App reads it on construction.
    from gui import app

    return app.main()


if __name__ == "__main__":
    raise SystemExit(main())
