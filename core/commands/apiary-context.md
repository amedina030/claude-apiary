---
name: apiary-context
description: Load apiary toolkit context (scribe, budgeter, runner, portability rules)
user-invocable: true
---

## CLI invocation — the launcher

All apiary CLI tools must be invoked via the launcher, which resolves the apiary repo path programmatically:

```bash
python ~/.claude/apiary_launch.py <relative-script-path> [args...]
```

The launcher reads `~/.claude/apiary.json` to locate the apiary repo, then runs the target script with its arguments forwarded. **The subprocess inherits the caller's cwd unchanged** — the launcher does NOT chdir into the apiary repo, so tools like scribe that use `git rev-parse --show-toplevel` resolve to the session's actual repo (where operational state should land). Apiary scripts find their own code via `Path(__file__)`, not cwd, so they don't need the chdir. This works from any directory — no `<repo_dir>` substitution needed.

To resolve the apiary repo path for Read tool targets (not CLI invocations), use:

```bash
python ~/.claude/apiary_launch.py --print-repo-path
```

Examples:
```bash
python ~/.claude/apiary_launch.py scribe/notes.py list --type todo
python ~/.claude/apiary_launch.py budgeter/report.py --since 7d
python ~/.claude/apiary_launch.py harden/round_counter.py start --session-id abc12345
```

## CLI tool lookup

When in doubt about a CLI tool's flags, look it up rather than guessing:

```bash
python ~/.claude/apiary_launch.py docs/reference/cli_lookup.py <tool>   # e.g. notes, report, round_counter
```

---

## Memory path

Canonical memory directory: `<state-dir>/scribe/memory/` where `<state-dir>` is the per-target dir resolved by the registry (`<apiary>/.repos/<name>-<id>/`). Do not write to the cwd-derived harness path.

---

## Compass personality profile

If the current target's compass profile exists, read it as part of loading this context. It describes the user's personality, behavior patterns, and quirks as inferred from prior sessions, and should inform how you respond — preferred verbosity, when to ask vs decide, communication style, autonomy tolerance, etc.

```bash
state_dir=$(python ~/.claude/apiary_launch.py core/utils/state.py)
test -f "$state_dir/compass/personality.md" && cat "$state_dir/compass/personality.md"
```

If the file is missing, do nothing (no error, no fallback). The personality profile is updated weekly by the compass synthesizer (`/compass-sync` for manual trigger). Treat its content as soft guidance — explicit user statements and `feedback`-type memory entries still override it.

---

## Portability

Code must work on Windows / macOS / Linux — see `PORTABILITY.md` before touching code, hooks, scripts, or `settings.json`.

---

## Notes, learnings, and memory

Apiary uses **scribe** (`scribe/notes.py`) for operational state (TODOs, handoffs, decisions, blockers, wishlists) and a **memory** directory for permanent facts.

**Read `scribe/CLAUDE.md` before writing notes, learnings, or making memory decisions.** Quick decision tree:

- Still true in 3 months -> **memory** (`<state-dir>/scribe/memory/`)
- Decays / operational / about current work -> **note** (`python ~/.claude/apiary_launch.py scribe/notes.py add --type ...`)
- Error workaround or non-obvious pattern -> **learning** (`python ~/.claude/apiary_launch.py scribe/notes.py learn`)

---

## Reference subsystems (lookup-on-demand)

Apiary has two per-repo reference stores. Neither is surfaced in the startup banner — reach for them when the task is research- or visual-adjacent, and let the user know what you found.

- **Researcher** (`researcher/cli.py`) — text findings (markdown + YAML frontmatter). Use for landscape surveys, evaluations, decisions made-from-research that may stay relevant for months. Look up: `researcher/cli.py find <query>`.
- **Captures** (`captures/cli.py`) — image + sidecar pairs. Use for screenshots (GUI iterations, UE viewport, design references) and any visual data the user wants tracked. Look up: `captures/cli.py find <query>`. Get a single image path to feed the Read tool: `captures/cli.py path <topic> <slug>`.

Both store state under `<repo-root>/.apiary/<tool>/` per-repo. Both use a controlled tag vocabulary in `<tool>/tags.yaml` — register tags via `register-tag` before adding entries that use them.

---

## List-form subprocess for long CLI arguments

When invoking a CLI tool with a text argument longer than ~3 lines or containing markdown, **always** use list-form subprocess -- never bash with shell quoting (backticks trigger command substitution, apostrophes break quoting).

```python
subprocess.run(["python", os.path.expanduser("~/.claude/apiary_launch.py"),
                 "scribe/notes.py", "add", "--type", "handoff",
                 "--summary", short_summary_var,
                 "--content", long_text_var], ...)
```

**Never:** `python scribe/notes.py add --content "text with `backticks` and it's broken"`
