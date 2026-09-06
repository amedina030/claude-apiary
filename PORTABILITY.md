# Portability

claude-apiary is designed to run on Windows, macOS, and Linux without per-OS branching. This document covers the prereqs, the bootstrap flow for a fresh clone, what state lives where, and the portability rules that contributors must follow.

## Prerequisites

- **Python ≥ 3.11** on `PATH` as `python` (or `python3`).
- **git** on `PATH`.
- A POSIX-style shell (bash on macOS/Linux, Git Bash on Windows) for anything apiary *runs for you*: the repo-local git hooks (`docs/hooks/*`, `runner/hooks/*`) and `scripts/*.sh` are Unix shell and are not `cmd.exe`/PowerShell scripts. What you type yourself is not restricted — `scripts/install.ps1` and `scripts/update.ps1` are the Windows install path, and the launcher idiom (`$(git rev-parse --show-toplevel)`) resolves identically in bash and PowerShell.
- Write access to the directory you cloned `claude-apiary` into, plus any repo you bootstrap apiary into. Apiary writes nothing to `~/.claude/` post-migration.

That's it — no system services, no daemons, no compiled extensions.

## Install (fresh clone)

```bash
git clone <repo-url> claude-apiary
cd claude-apiary

# Preferred: Poetry (creates a virtualenv, locks dependencies, registers the `apiary` console_script)
poetry install

# Alternative: pip (no lockfile)
pip install -r requirements.txt

# Bootstrap main-apiary against itself
poetry run apiary self-bootstrap
```

`apiary self-bootstrap` is idempotent. It:

- Creates `<main-apiary>/.repos/registry.json` if missing (initializes the bootstrapped-repo registry).
- Generates main-apiary's per-repo files at `<main-apiary>/.claude/apiary/{launch.py, main-apiary-pointer.json, self-pointer.json, version.json}`.
- Allocates uid 1 to main-apiary in the registry.
- Writes hooks into `<main-apiary>/.claude/settings.json` that invoke the per-repo launcher.
- Adds the apiary-managed zone to `<main-apiary>/CLAUDE.md`.

To bootstrap any other repo so apiary acts in it:

```bash
poetry run apiary install --target /path/to/your/repo
```

Sessions in non-bootstrapped repos run as vanilla Claude Code with no apiary hooks — apiary is fully opt-in per-repo.

## State: what's local, what's in the repo

Two storage locations, two purposes.

### In the repo (synced via git)

- Source code, hooks, slash commands, tests.
- `.claude-project-key` — the stable directory name for this repo's local state.
- Standards, docs, runner backlog.

Anything checked into git is shared across all clones of the repo.

### Centralized per-target state (`<apiary>/.repos/<name>-<id>/`)

Per-target state lives under the apiary checkout, not inside each target repo. The launcher resolves it via `git rev-parse` on the session's cwd, looks the repo up in `<apiary>/.repos/registry.json`, and exports `APIARY_TARGET_STATE_DIR` for child processes. A breadcrumb at `<target>/.apiary/pointer` lets passive consumers reverse-resolve.

- `<state-dir>/scribe/<type>/<year>/index.jsonl` — scribe's operational notes (e.g. `todos/2026/index.jsonl`, `handoffs/2026/index.jsonl`)
- `<state-dir>/scribe/<type>/<year>/<seq>.md` — per-note body files
- `<state-dir>/scribe/<type>/<year>/archive/` — auto-archived notes. Retention is per note type, not a flat clock: a handoff is superseded by the next one for the same `(role, mission)`, `context` and `decision` notes age out on their own schedules, a note marked `done` ages from when it was closed, and everything else is kept until it is closed. The constants live in `scribe/policy.py`; the generated table is in [docs/reference/file-storage.md](docs/reference/file-storage.md#scribe-data).
- `<state-dir>/scribe/learnings/<year>/` — accumulated project learnings (same typed-year layout)
- `<state-dir>/scribe/memory/` — long-lived memory facts loaded at session start
- `<state-dir>/compass/` — captured turn pairs, classified events and the generated rule table (`rules.md`)
- `<state-dir>/research/` — researcher entries + tag vocabulary
- `<state-dir>/captures/` — image + sidecar pairs
- `<state-dir>/runner/` — runner artifacts (intake, specs, plans, executions, etc.)
- `<state-dir>/bootstrap_state.json` — `apiary install` provenance record
- `<repo-root>/.apiary/pointer` — JSON breadcrumb pointing at the apiary repo + target id

Session transcripts (`~/.claude/projects/<key>/*.jsonl`) are Claude Code's and stay under `~/.claude/`. Apiary's own per-session files live elsewhere: identity and session history under `<main-apiary>/.repos/<slug>/sessions/`, once-per-session hook flags under `<repo>/.claude/apiary/session-tmp/`.

**This state is intentionally per-checkout and is not portable.** Notes, learnings, memory, and budgeter logs reflect what *this* checkout's Claude has been working on, with paths, session IDs, and timing rooted in that machine's history. Copying them to another machine usually creates more confusion than value (stale paths, dangling session references, conflicting handoffs). If you switch machines, start fresh on the new one — the repo is the source of truth, the local state is short-horizon scratchpad.

If you have a specific reason to move a single artifact (e.g. one decision note you want to carry forward), copy it by hand. There is deliberately no export/import script.

## Portability rules for contributors

If you're writing or editing hooks, skills, scripts, or settings in this repo, you must follow these rules so the codebase stays portable. This is the canonical list: it lives here, in the repo, so a contributor who is not this machine's owner can read it.

- **No absolute paths.** Never hard-code `C:\Users\…`, `/Users/…`, `/home/…`, or interpreter paths like `python.exe`. In Python, derive from `pathlib.Path(__file__).resolve().parent`. For user home, use `Path.home()`.
- **Hook commands use the per-repo launcher.** Hook commands in a bootstrapped repo's `<repo>/.claude/settings.json` must use `python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" <relative-path>`. Claude Code expands `$CLAUDE_PROJECT_DIR` at hook-fire time so the launcher resolves to the bootstrapped repo's own copy. The launcher reads `<repo>/.claude/apiary/main-apiary-pointer.json` to find main-apiary; hooks fail-safe (exit 0 with stderr warning) when main-apiary is unreachable.
- **Skill CLI invocations use the per-repo launcher via `$(git rev-parse --show-toplevel)`.** Skill templates (`.md` files in `*/commands/`) must invoke apiary CLI tools via `python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" <relative-path> [args...]` — never `<repo_dir>` placeholders or bare relative paths. Do **not** use `$CLAUDE_PROJECT_DIR` in skill templates: it is a hooks-only variable that the harness injects into hook command environments, but it is **empty** in the general Bash/PowerShell tool shell where skills run (the path then collapses to `/.claude/apiary/launch.py` and fails). `$(...)` command substitution works identically in Bash and PowerShell and resolves from any cwd, since the session's working dir is always inside a git repo. The launcher finds main-apiary, sets `APIARY_MAIN_REPO`, and forwards all arguments. This eliminates LLM-dependent path resolution and makes skills work in any bootstrapped repo. (Hook commands are the exception — see the rule above — because the harness sets `$CLAUDE_PROJECT_DIR` at hook-fire time.)
- **Skill Read-tool paths use `--print-repo-path`.** When a skill template needs Claude to Read a file from main-apiary (not invoke a CLI tool), resolve the path first: `python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" --print-repo-path`. Use the output as the prefix for Read tool paths. Never use `<repo_dir>` placeholders for this — they depend on LLM substitution which is fragile and non-portable.
- **No external binaries for core checks.** Tooling that runs on every commit must be stdlib Python. The secret scanner (`scripts/secret_scan.py`) is deliberately hand-rolled rather than wrapping `gitleaks` or `trufflehog`. The binding constraint is not "Go binary" — a pure-Python scanner like `detect-secrets` would not help either — it is that **git hooks resolve `py -3`/`python3`/`python`, not the Poetry virtualenv**, so nothing outside the standard library is importable on the hook path. There is no optional-escalation mode; if one is ever added it must stay opt-in and must never be the only check.
- **Git hooks resolve Python by probe, not by name.** `docs/hooks/*` must use the `py -3` / `python3` / `python` probe with an `$APIARY_PYTHON` override, since no single interpreter name exists on all three platforms. Git runs hooks through its bundled `sh` on Windows, so hook scripts follow the same shell-hygiene rules as everything else.
- **Null device:** use `os.devnull` or `subprocess.DEVNULL`. Never write the OS-specific literal — it differs by platform.
- **Subprocess:** list-form (`["git", "status"]`), never `shell=True`, never `.exe` suffixes.
- **Paths:** `pathlib.Path` end-to-end. Never concatenate with `/` or `\` literals.
- **File I/O:** explicit `encoding='utf-8'` on every `open()`, `read_text()`, `write_text()`.
- **Shell hygiene:** scripts that shell out must work under bash on Windows, macOS, and Linux. Use `/dev/null` not `NUL`; forward slashes in paths; no PowerShell or `cmd.exe` builtins.

If you spot a violation while doing other work, file a scribe todo rather than fixing it inline: portability changes land as coordinated sweeps (one rule at a time, across every caller), and ad-hoc patches leave half the tree on the old pattern.

## Troubleshooting

**`python scripts/preflight.py` complains about Python version.**
You're on a Python older than 3.11. Install a newer version and make sure `python` on your `PATH` resolves to it. Check with `python --version`.

**Missing packages warning from preflight.**
Run `poetry install` (or `pip install -r requirements.txt`). If you use a virtualenv, make sure it's activated before installing.

**`CLAUDE_PROJECT_DIR` not set in hook commands.**
Claude Code sets this automatically when invoking hooks. If you're running a hook manually for testing, set it to the repo root: `CLAUDE_PROJECT_DIR=$(pwd) python core/hooks/<hook>.py`.

**Hooks fail with "can't open file" or "main-apiary not reachable".**
The per-repo launcher at `<repo>/.claude/apiary/launch.py` reads `main-apiary-pointer.json` to find main-apiary. If main-apiary moved (or the pointer is stale), re-run `poetry run apiary install --target <repo>` from inside main-apiary, or open a Claude session in main-apiary first to trigger cascade-fix. If `<repo>/.claude/apiary/launch.py` doesn't exist, the repo isn't bootstrapped — run `apiary install --target <repo>`.

**Bootstrap refuses to seed and tells me to migrate first.**
You have scribe state at a legacy location — either `<repo-root>/.apiary/scribe/` (the pre-centralisation in-repo layout) or Claude Code's `~/.claude/projects/<key>/` — while the centralized `<state-dir>/scribe/` is empty. Move the legacy data into the centralized layout before re-running bootstrap. There is no `APIARY_STATE_LAYOUT=legacy` escape hatch any more; it was removed with the global tree.

**Hook scripts work on macOS/Linux but fail on Windows.**
Make sure they run through Git Bash (or WSL), not PowerShell or `cmd.exe` — Claude Code on Windows expects bash for hook commands. A `bad interpreter` error usually means CRLF line endings: `.gitattributes` pins `docs/hooks/*`, `runner/hooks/*` and `*.sh` to LF, and CI rejects a CRLF shebang in any of them.

**Notes from another machine I copied over look broken.**
Per the state-locality section, scribe state is intentionally not portable. Don't copy `<apiary>/.repos/` (or the legacy `<repo-root>/.apiary/` and `~/.claude/projects/<key>/` paths) between machines.
