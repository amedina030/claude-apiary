#!/usr/bin/env python3
"""CLI wrapper: print total tokens for a given request_id to stdout."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from budgeter.lib import logger, query


def main():
    parser = argparse.ArgumentParser(description="Sum tokens for a request_id from the budgeter log.")
    parser.add_argument("--request-id", required=True, help="APIARY_REQUEST_ID to query")
    parser.add_argument("--cwd", default=None, help="Project working directory (selects project log)")
    args = parser.parse_args()

    try:
        if args.cwd:
            cwd_path = Path(args.cwd)
            if not cwd_path.is_dir():
                print(f"--cwd is not an existing directory: {args.cwd}", file=sys.stderr)
                sys.exit(2)
            logger.configure_for_project(args.cwd)
        total = query.total_tokens_for_request(args.request_id)
        print(total)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
