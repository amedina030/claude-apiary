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

- Creates `~/.claude/` and `~/.claude/projects/<key>/` (where `<key>` comes from `.claude-project-key` at repo root — currently `claude-apiary`).
- Seeds empty `notes.jsonl`, `learnings.jsonl`, and `memory/MEMORY.md` so the scribe and memory systems have something to read.
- Creates `~/.claude/auto-startup-enabled` so the startup hook runs on first session.
- Verifies your Python version meets the minimum and warns if any package in `requirements.txt` fails to import.

On re-run it reports everything as "already present" and exits 0.

If you run `bootstrap.py` on a machine that already has scribe state under the legacy cwd-derived project key (e.g. `D--Professional-claude-apiary/`), it will refuse to seed and tell you to run `python scripts/migrate_project_key.py` first. That's a one-time migration to the stable key.

## State: what's local, what's in the repo

Two storage locations, two purposes.

### In the repo (synced via git)

- Source code, hooks, slash commands, tests.
- `.claude-project-key` — the stable directory name for this repo's local state.
- Standards, docs, runner backlog.

Anything checked into git is shared across all clones of the repo.

### Local to each machine (`~/.claude/projects/<key>/`)

- `notes.jsonl` — scribe's operational notes (TODOs, decisions, handoffs, context)
- `notes_archive.jsonl` — auto-archived old notes
- `learnings.jsonl` — accumulated project learnings
- `memory/` — long-lived memory facts loaded at session start
- Plus session transcripts and identity files written by Claude Code itself

**This state is intentionally per-machine and is not portable.** Notes, learnings, memory, and budgeter logs reflect what *this* machine's Claude has been working on, with paths, session IDs, and timing rooted in that machine's history. Copying them to another machine usually creates more confusion than value (stale paths, dangling session references, conflicting handoffs). If you switch machines, start fresh on the new one — the repo is the source of truth, the local state is short-horizon scratchpad.

If you have a specific reason to move a single artifact (e.g. one decision note you want to carry forward), copy it by hand. There is deliberately no export/import script.

## Portability rules for contributors

If you're writing or editing hooks, skills, scripts, or settings in this repo, you must follow these rules so the codebase stays portable. The full canonical list lives in the user's global `CLAUDE.md`; the load-bearing items are reproduced here.

- **No absolute paths.** Never hard-code `C:\Users\…`, `/Users/…`, `/home/…`, or interpreter paths like `python.exe`. In `settings.json` hook commands, use `$CLAUDE_PROJECT_DIR`. In Python, derive from `pathlib.Path(__file__).resolve().parent`. For user home, use `Path.home()`.
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

**Bootstrap refuses to seed and tells me to migrate first.**
You have scribe state under the legacy cwd-derived key. Run `python scripts/migrate_project_key.py` to move it to the stable key, then re-run bootstrap.

**Hook scripts work on macOS/Linux but fail on Windows.**
Make sure you're running them through Git Bash (or WSL), not PowerShell or `cmd.exe`. Claude Code on Windows expects bash for hook commands.

**Notes from another machine I copied over look broken.**
Per the state-locality section, scribe state is intentionally not portable. Don't copy `~/.claude/projects/<key>/` between machines.
