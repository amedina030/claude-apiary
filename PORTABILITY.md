# Portability

claude-apiary is designed to run on Windows, macOS, and Linux without per-OS branching. This document covers the prereqs, the bootstrap flow for a fresh clone, what state lives where, and the portability rules that contributors must follow.

## Prerequisites

- **Python ≥ 3.11** on `PATH` as `python` (or `python3`).
- **git** on `PATH`.
- A POSIX-style shell (bash on macOS/Linux, Git Bash on Windows). The hooks and scripts use Unix shell syntax — `cmd.exe` and PowerShell are not supported.
- Write access to `~/.claude/` (created on first run if missing).

That's it — no system services, no daemons, no compiled extensions.

## Bootstrap (fresh clone)

```bash
git clone <repo-url> claude-apiary
cd claude-apiary
pip install -r requirements.txt
python scripts/bootstrap.py
```

`bootstrap.py` is idempotent: safe to run as many times as you want. On first run it:

- Creates `~/.claude/` (for Claude Code's own per-user state — identity files, transcripts, the auto-startup flag).
- Creates the in-repo umbrella state directory `<repo-root>/.apiary/` and writes `<repo-root>/.apiary/.gitignore` containing `*` so the whole umbrella self-ignores.
- Creates `<repo-root>/.apiary/scribe/` and seeds empty `notes.jsonl`, `learnings.jsonl`, and `memory/MEMORY.md` so the scribe and memory systems have something to read.
- Creates `~/.claude/auto-startup-enabled` so the startup hook runs on first session.
- Verifies your Python version meets the minimum and warns if any package in `requirements.txt` fails to import.

On re-run it reports everything as "already present" and exits 0.

If you run `bootstrap.py` on a machine that already has scribe state under the legacy `~/.claude/projects/<key>/` location and the in-repo `<repo-root>/.apiary/scribe/` is empty, bootstrap refuses to seed and tells you to run `python scripts/migrate_scribe_state.py --source <legacy-dir>` first. That's the one-shot in-repo migration from decision #269.

## State: what's local, what's in the repo

Two storage locations, two purposes.

### In the repo (synced via git)

- Source code, hooks, slash commands, tests.
- `.claude-project-key` — the stable directory name for this repo's local state.
- Standards, docs, runner backlog.

Anything checked into git is shared across all clones of the repo.

### Local to each checkout (`<repo-root>/.apiary/`)

Scribe state lives in the repo checkout under the umbrella `.apiary/` directory, which self-ignores via `.apiary/.gitignore` (contents: `*`). Each subdirectory is owned by one apiary tool:

- `<repo-root>/.apiary/scribe/notes.jsonl` — scribe's operational notes (TODOs, decisions, handoffs, context)
- `<repo-root>/.apiary/scribe/notes_archive.jsonl` — auto-archived old notes
- `<repo-root>/.apiary/scribe/learnings.jsonl` — accumulated project learnings
- `<repo-root>/.apiary/scribe/backfill_skip.json` — sessions skipped from unseen-session detection
- `<repo-root>/.apiary/scribe/memory/` — long-lived memory facts loaded at session start
- `<repo-root>/.apiary/hooks/` — hook runtime state (sanitizer hit log, etc.)

Session transcripts and identity files written by Claude Code itself stay under `~/.claude/` — those belong to Claude Code, not apiary.

**This state is intentionally per-checkout and is not portable.** Notes, learnings, memory, and budgeter logs reflect what *this* checkout's Claude has been working on, with paths, session IDs, and timing rooted in that machine's history. Copying them to another machine usually creates more confusion than value (stale paths, dangling session references, conflicting handoffs). If you switch machines, start fresh on the new one — the repo is the source of truth, the local state is short-horizon scratchpad.

**Migration status.** The in-repo layout is gated on `APIARY_STATE_LAYOUT=repo` during the migration window. The legacy path `~/.claude/projects/<project-key>/` remains the default until todo #268 flips it and cleans up the old location. Decision #269 and todos #262–#268 track the work. Data was mirrored into the new layout in step #266, but writes currently still hit the legacy store.

If you have a specific reason to move a single artifact (e.g. one decision note you want to carry forward), copy it by hand. There is deliberately no export/import script.

## Portability rules for contributors

If you're writing or editing hooks, skills, scripts, or settings in this repo, you must follow these rules so the codebase stays portable. The full canonical list lives in the user's global `CLAUDE.md`; the load-bearing items are reproduced here.

- **No absolute paths.** Never hard-code `C:\Users\…`, `/Users/…`, `/home/…`, or interpreter paths like `python.exe`. In Python, derive from `pathlib.Path(__file__).resolve().parent`. For user home, use `Path.home()`.
- **Hook commands use the launcher.** Global hook commands in `settings.json` must use `python ~/.claude/apiary_launch.py <relative-path>` — never `$CLAUDE_PROJECT_DIR` (which resolves to the *session's* repo, not apiary) or absolute paths. The launcher reads `~/.claude/apiary.json` to find the apiary repo, making hooks work from any repo. Source of truth: `core/apiary_launch.py`, copied to `~/.claude/` by `setup.py --global`.
- **Skill CLI invocations use the launcher.** Skill templates (`.md` files in `*/commands/`) must invoke apiary CLI tools via `python ~/.claude/apiary_launch.py <relative-path> [args...]` — never `<repo_dir>` placeholders or bare relative paths. The launcher finds the repo, sets `cwd` and `CLAUDE_PROJECT_DIR`, and forwards all arguments. This eliminates LLM-dependent path resolution and makes skills work from any directory.
- **Null device:** use `os.devnull` or `subprocess.DEVNULL`. Never write the OS-specific literal — it differs by platform.
- **Subprocess:** list-form (`["git", "status"]`), never `shell=True`, never `.exe` suffixes.
- **Paths:** `pathlib.Path` end-to-end. Never concatenate with `/` or `\` literals.
- **File I/O:** explicit `encoding='utf-8'` on every `open()`, `read_text()`, `write_text()`.
- **Shell hygiene:** scripts that shell out must work under bash on Windows, macOS, and Linux. Use `/dev/null` not `NUL`; forward slashes in paths; no PowerShell or `cmd.exe` builtins.

If you spot a violation while doing other work, file a follow-up rather than fixing it inline — the portability epic landed in coordinated phases (T5a–T5d) and ad-hoc patches tend to leak.

## Troubleshooting

**`python scripts/bootstrap.py` complains about Python version.**
You're on a Python older than 3.11. Install a newer version and make sure `python` on your `PATH` resolves to it. Check with `python --version`.

**Missing packages warning from bootstrap.**
Run `pip install -r requirements.txt`. If you use a virtualenv, make sure it's activated before bootstrapping.

**`CLAUDE_PROJECT_DIR` not set in hook commands.**
Claude Code sets this automatically when invoking hooks. If you're running a hook manually for testing, set it to the repo root: `CLAUDE_PROJECT_DIR=$(pwd) python core/hooks/<hook>.py`.

**Hooks fail with "can't open file" from a different repo.**
Global hooks use `~/.claude/apiary_launch.py` to locate the apiary repo via `~/.claude/apiary.json`. If either file is missing, re-run `python setup.py --global` from the apiary repo to install them. If you see `$CLAUDE_PROJECT_DIR` in the failing command, your hooks are using the old format — re-running setup will replace them with launcher-based commands.

**Bootstrap refuses to seed and tells me to migrate first.**
You have scribe state under a legacy `~/.claude/projects/<key>/` location and the in-repo `<repo-root>/.apiary/scribe/` is empty. Run `python scripts/migrate_scribe_state.py --source <legacy-dir>` (or just `--dry-run` first to preview) to copy it into the umbrella, then re-run bootstrap.

**Hook scripts work on macOS/Linux but fail on Windows.**
Make sure you're running them through Git Bash (or WSL), not PowerShell or `cmd.exe`. Claude Code on Windows expects bash for hook commands.

**Notes from another machine I copied over look broken.**
Per the state-locality section, scribe state is intentionally not portable. Don't copy `<repo-root>/.apiary/` (or the legacy `~/.claude/projects/<key>/`) between machines.
