#!/usr/bin/env python3
"""Apiary setup — redirect stub.

The original ``setup.py --global`` / ``--project-path`` install paths
were removed during the per-repo migration (see ``MIGRATION-PLAN.md``
§10 phase 5). The new entry points are:

- Bootstrap a repo:        ``poetry run apiary install --target <repo>``
- Bootstrap main-apiary:   ``poetry run apiary self-bootstrap``
- Validate the install:    ``poetry run apiary doctor``
- Repo-local git hooks:    ``python scripts/install_repo_hooks.py``

This stub catches users running old commands and points them at the new
ones. It exits with code 1 to signal that the operation didn't run.
"""
from __future__ import annotations

import sys


_REDIRECTS = {
    "--global": (
        "`setup.py --global` is gone. Apiary now installs per-repo:\n"
        "  poetry run apiary self-bootstrap            # main-apiary itself\n"
        "  poetry run apiary install --target <repo>   # any other repo"
    ),
    "--project-path": (
        "`setup.py --project-path <path>` is gone. Run:\n"
        "  poetry run apiary install --target <path>"
    ),
    "--check": (
        "`setup.py --check` is gone. Run:\n"
        "  poetry run apiary doctor"
    ),
}


def main() -> int:
    flags_in_argv = [a for a in sys.argv[1:] if a.startswith("--")]
    for flag in flags_in_argv:
        # Strip any "=value" suffix for matching
        bare = flag.split("=", 1)[0]
        if bare in _REDIRECTS:
            print(_REDIRECTS[bare], file=sys.stderr)
            return 1
    print(
        "setup.py is a redirect stub after the per-repo migration. Run:\n"
        "  poetry run apiary --help\n"
        "for the current CLI. See MIGRATION-PLAN.md for context.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
