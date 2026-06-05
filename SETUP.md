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

This installs `.git/hooks/pre-commit` (runs `docs/check.py`) and `.git/hooks/post-merge` (closes scribe TODOs linked to merged runner branches) into main-apiary's own `.git/hooks/`. Repo-local — unrelated to Claude Code hooks.

### 5. Start a new Claude Code session

Hooks and slash commands are loaded at session start. Restart Claude Code after bootstrap.

### 6. Enable the features you want

Inside a bootstrapped repo:

```
/budgeter-log     # start recording token usage
/budgeter-warn    # enable expensive-call warnings
```

Toggles persist per-repo at `<repo>/.claude/apiary/flags/<flag-name>-enabled`.

---

## Updating

```bash
git pull
poetry install
poetry run apiary self-bootstrap                       # refresh main-apiary
poetry run apiary install --target /path/to/repo       # refresh each bootstrapped repo
poetry run apiary doctor                               # validate registry + pin files
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

**Everything:**

Uninstall each repo (above), then delete the `claude-apiary` checkout. Apiary writes nothing to `~/.claude/`.

---

## Troubleshooting

**Hooks not firing in a repo**
Confirm `<repo>/.claude/apiary/launch.py` exists. If not, run `apiary install --target <repo>` and start a new Claude session.

**`apiary doctor` reports `pointers` issues**
Main-apiary's self-pointer drifted. Run `apiary doctor pointers --fix` to update main-apiary's pointers and cascade-fix every bootstrapped repo's reference.

**`apiary doctor` reports `unreachable`**
A registered repo's `real_path` no longer exists on disk. Either restore the repo, or `apiary uninstall --target <real_path>` (the registry entry is removed even if the path is gone, freeing the slug).

**`/budgeter-log` toggle has no effect**
Check `<repo>/.claude/apiary/flags/budgeter-log-enabled` exists when ON, is absent when OFF. The slash command creates/removes this file.

**Warnings never firing**
Warnings require at least `min_tasks` unique tasks in the log (default: 50). Run `/budgeter-log` and use the repo until you've accumulated enough history.

**`setup.py --global` still in your muscle memory**
That mode is gone. The redirect stub at `setup.py` will print the new commands.
