# Setup Guide

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/claude-code) installed and configured
- [Poetry](https://python-poetry.org/) (recommended) or pip
- git (every bootstrapped repo must be a git repo — apiary uses `git rev-parse` to identify targets)

No external runtime dependencies — dev dependencies (pytest) are managed via `pyproject.toml`.

---

## Install Model

Apiary uses a **per-repo opt-in** install. Each repo you want apiary to act on is bootstrapped individually; sessions in non-bootstrapped repos run as vanilla Claude Code with no apiary hooks, no managed CLAUDE.md zone, no budgeter logging. The single `~/.claude/apiary*` global install model was retired in 2026-05 — see `MIGRATION-PLAN.md` for the full design.

What lives where after bootstrap:

- **Per-repo install state** — `<repo>/.claude/apiary/` (launcher, pointers, version, flags). Gitignored; regenerable.
- **Per-repo hooks** — `<repo>/.claude/settings.json` references `$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py`.
- **Per-repo slash commands** — `<repo>/.claude/commands/*.md`, copied at install time.
- **Per-target data** — centralized at `<main-apiary>/.repos/<name>-<uid>/` (scribe notes, sessions, runner state, compass observations, captures, researcher findings).
- **Apiary code** — your `claude-apiary` clone (referred to as **main-apiary**).

---

## New Machine Install

### Quick install (recommended)

After cloning, run the one-command installer for your OS. It runs a preflight
environment check (reporting any blockers up front), finds a real Python,
ensures Poetry, installs dependencies, and runs the whole bootstrap chain
(`self-bootstrap` → repo hooks → `doctor`) as a single user action:

```powershell
# Windows (PowerShell), from inside the clone:
.\scripts\install.ps1            # CLI only
.\scripts\install.ps1 -Gui       # CLI + desktop GUI
```

```bash
# macOS / Linux, from inside the clone:
./scripts/install.sh             # CLI only
./scripts/install.sh --gui       # CLI + desktop GUI
```

Add `-Gui` / `--gui` to also set up the desktop app: it pulls the `gui` Poetry
group and prefers a GUI-compatible interpreter (Python 3.11/3.12 — see
[The desktop GUI](#the-desktop-gui-optional)). Want to inspect first? The Windows
installer takes `-DryRun` to print every step without changing anything.

Why a script instead of running the steps by hand: on Windows the installer
finds Python the robust way (the `py` launcher + the registry, not a directory
guess) so an install in a non-standard location is still found, and it runs
every step with the WindowsApps alias stripped from PATH so Poetry's interpreter
discovery can't get hijacked (see [Troubleshooting](#troubleshooting)). Bundling
the steps into one user-run script also means the Claude Code safety classifier
won't block the bootstrap halfway through — see the permission note below.

> **Heads-up if an agent is doing the install for you.** `apiary self-bootstrap`
> runs code from a freshly cloned repo, so Claude Code's auto-approval classifier
> will (correctly) refuse to run it on its own — and an agent *cannot* grant
> itself the permission either (that's flagged as self-modification). The clean
> way through is to run `scripts/install.ps1` / `scripts/install.sh` **yourself**
> (e.g. `! .\scripts\install.ps1` in the composer), so the whole chain is clearly
> your action. The manual steps below work the same way.

The rest of this section documents the manual steps the installer automates.

### 1. Clone and install dependencies

```bash
git clone https://github.com/amedina030/claude-apiary.git /path/to/claude-apiary
cd /path/to/claude-apiary
poetry install        # registers the `apiary` console_script
```

### 2. Bootstrap main-apiary against itself

```bash
poetry run apiary self-bootstrap
```

This creates `<main-apiary>/.claude/apiary/{launch.py, main-apiary-pointer.json, self-pointer.json, version.json}`, registers main-apiary at `uid=1` in `<main-apiary>/.repos/registry.json`, and installs hooks into `<main-apiary>/.claude/settings.json`.

> **git-bash on Windows:** the `apiary` console script has a `#!...python.exe`
> shebang that git-bash can't honor (it tries to run the Python as shell and
> fails with `import: command not found`). Use the module form instead — it is
> shell-agnostic and works everywhere:
>
> ```bash
> poetry run python -m core.cli self-bootstrap
> ```

### 3. Bootstrap any other repo you want apiary in

```bash
poetry run apiary install --target /path/to/your/repo
```

For each target, this:

- Allocates a new uid in main-apiary's registry.
- Generates the per-repo files under `<target>/.claude/apiary/`.
- Writes `<target>/.claude/settings.json` from the resolved profile.
- Copies slash commands into `<target>/.claude/commands/`.
- Adds the apiary-managed zone to `<target>/CLAUDE.md`.
- Adds `.claude/` to `<target>/.gitignore` if not already there.

Idempotent — re-running refreshes generated files and updates hash records in `<main-apiary>/.repos/<slug>/bootstrap_state.json`.

### 4. Install repo-local git hooks (main-apiary only)

```bash
python scripts/install_repo_hooks.py
```

This installs `.git/hooks/pre-commit` and `.git/hooks/post-merge` into main-apiary's own `.git/hooks/`. Repo-local — unrelated to Claude Code hooks.

The pre-commit hook chains two checks, and either one failing blocks the commit:

- `docs/check.py` — framework doc conformance.
- `scripts/secret_scan.py --staged` — credentials in the staged diff (API keys,
  private keys, credential-shaped assignments), plus filenames that hold secrets
  by convention (`.env`, `id_rsa`, `*.pem`) even when `git add -f` bypasses
  `.gitignore`.

The post-merge hook closes scribe TODOs linked to merged runner branches.

If the secret scan flags a false positive, add an inline `apiary:allow-secret`
comment on that line, or an entry to the repo-root `.secretsallow` file (a path
regex exempts the file; `line:<regex>` exempts matching lines).
`git commit --no-verify` bypasses every pre-commit hook as a last resort. The
scan fails closed: if git itself cannot run, the commit is blocked with a
message saying the scan did not happen, rather than passing unscanned.

### 4b. The secret-scan hook in other repos

`apiary install --target <repo>` installs it as part of every bootstrap, so a
repo added in step 3 already has it — side projects get the secret scan alone,
since they have no framework docs to check.

Reach for the standalone installer only to retrofit a repo bootstrapped before
this was wired in, or to inspect / remove one:

```bash
python .claude/apiary/launch.py scripts/install_git_hooks.py
python .claude/apiary/launch.py scripts/install_git_hooks.py --list
python .claude/apiary/launch.py scripts/install_git_hooks.py --uninstall
```

An existing pre-commit hook that isn't apiary's is never overwritten; the
installer refuses and tells you, so inspect it and re-run with `--force`.

To find repos that predate the change, `poetry run apiary doctor` reports
registered repos, and `incubator/cli.py verify --path <repo>` includes the hook
in its checks.

### 5. Start a new Claude Code session

Hooks and slash commands are loaded at session start. Restart Claude Code after bootstrap.

### 6. Enable the features you want

Inside a bootstrapped repo:

```
/budgeter log     # start recording token usage
/budgeter warn    # enable expensive-call warnings
```

Toggles persist per-repo at `<repo>/.claude/apiary/flags/<flag-name>-enabled`.

---

## The desktop GUI (optional)

Apiary ships an optional desktop app (`gui/`) — a PyWebView window that wraps a
Claude Code session with a clean chat view, token counts, and a scribe sidebar.
It is **not** part of the base install; it has its own dependency group and a few
native prerequisites that a fresh machine often lacks. These gaps are the usual
reason "the GUI won't start after install."

### Requirements (read before installing)

- **Python 3.11 or 3.12 — not 3.13+.** The GUI's `pythonnet` dependency only has
  wheels for `>=3.11,<3.13`. On Python 3.13/3.14 `poetry install --with gui`
  resolves *without* `pythonnet` and the window fails to open. If your default
  interpreter is newer, install a 3.12 alongside it and point Poetry at it:
  `poetry env use /path/to/python3.12` before installing the group.
- **Microsoft Edge WebView2 Runtime** (Windows). PyWebView renders through it; if
  it's absent the window creation fails with a cryptic error. Most Windows 11
  machines have it; if not, install the Evergreen runtime from
  <https://developer.microsoft.com/microsoft-edge/webview2/>.
- **Claude Code on `PATH`.** The GUI spawns `claude` in a pty. It prefers a real
  `claude.exe` and automatically wraps an npm batch shim through `cmd.exe` (so
  the old `[WinError 193]` spawn failure no longer bites), but `claude` must be
  resolvable. If the window opens but tabs won't start, confirm Claude Code is
  installed, or set explicit `command`/`args` keys in
  `<main-apiary>/.apiary/gui/apiary_gui/launch.json`.

The GUI prints a startup warning to the terminal if WebView2 or `claude` is
missing — launch it from a shell the first time so you see those.

### Install and run

The simplest path is the installer's `-Gui` / `--gui` flag, which installs the
group and picks a GUI-compatible interpreter for you:

```powershell
.\scripts\install.ps1 -Gui        # Windows
./scripts/install.sh --gui        # macOS / Linux
```

Or set it up by hand inside an existing install:

```bash
poetry install --with gui          # adds pywebview, pythonnet, pywinpty, watchdog
poetry run python -m gui.app
```

Windows V1 only (the pty backend is `pywinpty`). See `gui/README.md` for profiles,
hot-reload, and the permission-prompt MCP path.

> The GUI does not hot-reload changes to its own source — after editing
> `gui/web/*` or the Python backend, fully restart the `gui.app` process.

---

## Updating

Updates are frequent during development. The simplest path is the updater — it
pulls the latest code and re-runs the idempotent install chain (`poetry install`,
`self-bootstrap` to refresh hooks + slash commands, repo hooks, `doctor`). Use
the **same flags you installed with**:

```powershell
.\scripts\update.ps1            # Windows: update CLI
.\scripts\update.ps1 -Gui       # Windows: update CLI + desktop GUI
```

```bash
./scripts/update.sh             # macOS / Linux: update CLI
./scripts/update.sh --gui       # macOS / Linux: update CLI + desktop GUI
```

The updater only touches main-apiary itself. To refresh **another** bootstrapped
repo (new hooks/commands after an upgrade), re-run the install for it:

```bash
poetry run apiary install --target /path/to/repo
poetry run apiary doctor                               # validate registry + pin files
```

Doing it fully by hand is equivalent to:

```bash
git pull
poetry install                                         # add --with gui for the GUI
poetry run apiary self-bootstrap                       # refresh main-apiary
poetry run apiary doctor
```

If main-apiary's pinned version (`<main-apiary>/VERSION`) advanced past a repo's pinned version (`<repo>/.claude/apiary/version.json`), `apiary doctor versions` flags it. The versioned migration runner under `<main-apiary>/migrations/` chains the upgrade scripts.

---

## Moving main-apiary

If you move the `claude-apiary` checkout, open a Claude session inside it. The drift handler detects that main-apiary's self-pointer no longer matches the actual location, updates main-apiary's own pointers, and **cascade-fixes** every bootstrapped repo's `<repo>/.claude/apiary/main-apiary-pointer.json` to the new path.

You can also force the cascade manually:

```bash
poetry run apiary cascade-fix
```

---

## Moving a bootstrapped repo

Open a Claude session inside the moved repo. The drift handler detects the move, queues an `update_path` message into main-apiary's mailbox at `<main-apiary>/.apiary/forwarding/<uid>.json`. On main-apiary's next session (or `apiary doctor mailbox --fix`), the registry's `real_path` is updated.

---

## Uninstalling

**Per-repo (one repo):**

```bash
poetry run apiary uninstall --target /path/to/repo               # keep data
poetry run apiary uninstall --target /path/to/repo --remove-data # also delete the repo's per-target state
```

This removes `<repo>/.claude/apiary/`, the apiary-installed slash commands, the apiary hook entries from `<repo>/.claude/settings.json`, and the apiary-managed zone in `<repo>/CLAUDE.md`. Without `--remove-data`, the `<main-apiary>/.repos/<slug>/` per-target state stays put for archival.

Repo-local **git** hooks are separate — `apiary uninstall` doesn't touch them,
because they live in `.git/hooks/` rather than in the managed zone. Remove the
secret-scan hook explicitly if you want it gone:

```bash
python .claude/apiary/launch.py scripts/install_git_hooks.py --uninstall
```

**Everything:**

Uninstall each repo (above), then delete the `claude-apiary` checkout. Apiary writes nothing to `~/.claude/`.

---

## Troubleshooting

**Windows: "Python was not found" even though Python is installed**
Two distinct things bite here. (1) Bare `python` / `python3` on Windows often
resolves to the **WindowsApps app-execution alias** — a stub that opens the
Microsoft Store and exits with code `9009`, which breaks Poetry's interpreter
discovery (`returned non-zero exit status 9009`). (2) A real install in a
non-standard location (the Store package, a custom dir, the newer
`%LOCALAPPDATA%\Programs\Python\…` / `%LOCALAPPDATA%\Python\…` layouts, or conda)
won't be found by guessing at directories. `scripts\install.ps1` handles both:
it enumerates interpreters via the `py` launcher and the registry
(`HKLM/HKCU\SOFTWARE\Python\PythonCore`), rejects the WindowsApps stub, and runs
every child process with WindowsApps stripped from PATH. To do it by hand, point
Poetry at the real interpreter and strip the alias for the session:

```powershell
$py = (py -3 -c "import sys; print(sys.executable)")
$env:PATH = ($env:PATH -split ';' | Where-Object { $_ -notlike '*WindowsApps*' }) -join ';'
poetry env use $py
poetry install
```

**Hooks not firing in a repo**
Confirm `<repo>/.claude/apiary/launch.py` exists. If not, run `apiary install --target <repo>` and start a new Claude session.

**`apiary doctor` reports `pointers` issues**
Main-apiary's self-pointer drifted. Run `apiary doctor pointers --fix` to update main-apiary's pointers and cascade-fix every bootstrapped repo's reference.

**`apiary doctor` reports `unreachable`**
A registered repo's `real_path` no longer exists on disk. Either restore the repo, or `apiary uninstall --target <real_path>` (the registry entry is removed even if the path is gone, freeing the slug).

**`/budgeter log` toggle has no effect**
Check `<repo>/.claude/apiary/flags/budgeter-log-enabled` exists when ON, is absent when OFF. The slash command creates/removes this file via `core/flags.py`; run `python core/flags.py status budgeter-log` from inside the repo to see what the hooks see.

**Warnings never firing**
Warnings require at least `min_tasks` unique tasks in the log (default: 50). Run `/budgeter log` and use the repo until you've accumulated enough history.

**`setup.py --global` still in your muscle memory**
That mode is gone. The redirect stub at `setup.py` will print the new commands.
