#!/usr/bin/env bash
# Fresh-machine installer for claude-apiary on macOS / Linux.
#
# One user-run script: finds a real Python >= 3.11, ensures Poetry, runs
# `poetry install`, then drives self-bootstrap -> repo hooks -> doctor. The CLI
# is invoked as `python -m core.cli` (shell-agnostic) rather than the `apiary`
# console script. The Windows WindowsApps-stub problem does not exist here; the
# only "installed but unfindable" case on unix is python living under a name or
# manager (pyenv) the caller didn't think to check, which the search below
# covers.
#
# Usage:
#   ./scripts/install.sh [--gui] [--skip-bootstrap] [--yes]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MIN_MAJOR=3
MIN_MINOR=11

SKIP_BOOTSTRAP=0
ASSUME_YES=0
WITH_GUI=0
for arg in "$@"; do
    case "$arg" in
        --gui) WITH_GUI=1 ;;
        --skip-bootstrap) SKIP_BOOTSTRAP=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m    OK  %s\033[0m\n' "$1"; }
die()  { printf '\033[31m    X   %s\033[0m\n' "$1" >&2; exit 1; }

# Return 0 if $1 is a runnable python >= MIN.
is_good_python() {
    local exe="$1"
    [ -n "$exe" ] || return 1
    command -v "$exe" >/dev/null 2>&1 || [ -x "$exe" ] || return 1
    "$exe" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($MIN_MAJOR,$MIN_MINOR) else 1)" >/dev/null 2>&1
}

find_python() {
    # Explicit version names first, then generic, then pyenv's active shim.
    # With --gui, prefer 3.12/3.11 (pythonnet has no 3.13+ wheel) before any
    # generic name that might resolve to a newer interpreter.
    local names="python3.13 python3.12 python3.11 python3 python"
    if [ "$WITH_GUI" -eq 1 ]; then
        names="python3.12 python3.11 python3 python"
    fi
    for name in $names; do
        local p
        p="$(command -v "$name" 2>/dev/null || true)"
        if [ -n "$p" ] && is_good_python "$p"; then echo "$p"; return 0; fi
    done
    if command -v pyenv >/dev/null 2>&1; then
        local p
        p="$(pyenv which python 2>/dev/null || true)"
        if [ -n "$p" ] && is_good_python "$p"; then echo "$p"; return 0; fi
    fi
    return 1
}

step "Locating a real Python >= ${MIN_MAJOR}.${MIN_MINOR}"
PY="$(find_python || true)"
if [ -z "$PY" ]; then
    die "No Python >= ${MIN_MAJOR}.${MIN_MINOR} found. Install one (e.g. 'brew install python@3.12' or your distro's package) and re-run."
fi
ok "Python $("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])'): $PY"

step "Running pre-install environment check"
if [ "$WITH_GUI" -eq 1 ]; then
    "$PY" "$REPO_ROOT/scripts/preflight.py" --gui || die "Preflight found blockers (above). Resolve them and re-run."
else
    "$PY" "$REPO_ROOT/scripts/preflight.py" || die "Preflight found blockers (above). Resolve them and re-run."
fi

step "Ensuring Poetry is installed"
if command -v poetry >/dev/null 2>&1; then
    ok "Poetry already installed: $(command -v poetry)"
    POETRY=poetry
else
    if [ "$ASSUME_YES" -ne 1 ]; then
        read -r -p "    Install Poetry via 'pip install --user poetry'? [Y/n] " ans
        case "${ans:-y}" in [nN]*) die "Poetry is required. Aborting." ;; esac
    fi
    "$PY" -m pip install --user poetry
    POETRY="$("$PY" -m site --user-base)/bin/poetry"
    [ -x "$POETRY" ] || die "Poetry installed but not found at $POETRY."
    ok "Poetry installed: $POETRY"
fi

cd "$REPO_ROOT"
step "Pointing Poetry at $PY and installing dependencies"
"$POETRY" env use "$PY"
if [ "$WITH_GUI" -eq 1 ]; then
    "$POETRY" install --with gui
    ok "poetry install complete (with gui)"
else
    "$POETRY" install
    ok "poetry install complete"
fi

if [ "$SKIP_BOOTSTRAP" -eq 1 ]; then
    printf '\033[33m    !   --skip-bootstrap set; stopping after dependency install.\033[0m\n'
    exit 0
fi

step "Bootstrapping main-apiary against itself"
"$POETRY" run python -m core.cli self-bootstrap
ok "self-bootstrap complete"

step "Installing repo-local git hooks"
"$POETRY" run python scripts/install_repo_hooks.py
ok "git hooks installed"

step "Validating the install"
"$POETRY" run python -m core.cli doctor

printf '\033[32m\nDone. Restart Claude Code in a session rooted at this repo so its hooks\n'
printf 'and slash commands load. Then run /apiary-context if anything is missing.\033[0m\n'
if [ "$WITH_GUI" -eq 1 ]; then
    printf '\033[32mLaunch the desktop GUI with:  poetry run python -m gui.app\033[0m\n'
fi
