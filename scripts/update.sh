#!/usr/bin/env bash
# Update an existing claude-apiary install on macOS / Linux (repo + GUI).
#
# Pulls the latest code, then re-runs the idempotent install chain
# (poetry install, self-bootstrap, repo hooks, doctor). Counterpart to
# scripts/install.sh; forwards the same flags. Safe to run as often as you like.
#
# Usage:
#   ./scripts/update.sh [--gui] [--skip-bootstrap] [--yes]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

printf '\033[35m\nclaude-apiary updater (macOS/Linux)\033[0m\n'
printf 'repo: %s\n\n' "$REPO_ROOT"

printf '\033[36m==> Pulling latest (git pull --ff-only)\033[0m\n'
if ! git -C "$REPO_ROOT" pull --ff-only; then
    printf '\033[31m    X   git pull was not a fast-forward. Resolve manually (git status / git stash), then re-run.\033[0m\n' >&2
    exit 1
fi

# install.sh is idempotent (deps, self-bootstrap, hooks, doctor) and accepts the
# same flags, so updates reuse exactly one implementation.
exec "$SCRIPT_DIR/install.sh" "$@"
