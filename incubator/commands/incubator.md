---
name: incubator
description: Spawn a new side-project repo wired up with the apiary toolkit — refines the idea, creates a git repo, drops a Python+poetry skeleton, migrates the spec into the new repo's scribe
user-invocable: true
---

# /incubator — Spawn a new side-project repo

Take a fuzzy idea for a side project and turn it into a fresh, ready-to-work repo: refine the idea via `/refine`, create a git repo at a user-supplied path, lay down a Python+poetry skeleton, and migrate the refine spec into the new repo's scribe.

This is an orchestrator — it delegates the *what* (refining the idea into a spec) to `/refine` and the *how* (file/git mechanics) to `incubator/cli.py spawn`. It does not produce the spec itself.

## Arguments

- `/incubator <idea>` — start with the given idea
- `/incubator` (no args) — ask the user what they want to build first
- `/incubator cancel` — cancel mid-flow (delegates to `/refine cancel` if refine is active)

---

## Step 1: Get the idea

If no argument was given, ask the user: "What's the idea?" Wait for a response before proceeding.

If the user wrote `cancel`, delegate cancellation to `/refine` (`/refine cancel`) and stop.

---

## Step 2: Refine the idea

Invoke the `/refine` skill with the user's idea. Let `/refine` run its full flow (value challenge, question rounds, handoff, save). Do not interrupt or short-circuit it.

When `/refine` saves the spec, its final tool output will include a line like `Added C-YYYY-NN (context)`. **Capture that note ID** — you'll need it for the spawn step. If you can't see the ID in the output, query scribe to find the most recent context note for this session:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py list --type context --last 1
```

If `/refine` killed the idea (insufficient grounding), exit cleanly. Do not proceed to spawn.

---

## Step 3: Ask for a target path

Once the spec is saved, ask the user for an **absolute path** to the new repo's directory. The directory must not exist yet — its parent must.

Phrase the question concretely, e.g.: "Where should the new repo go? Give me an absolute path that doesn't exist yet (e.g. `D:\Professional\food-tracker`)."

If the user gives a relative path or a path that already exists, point it out and ask again. Do not silently fix.

---

## Step 4: Spawn

Call the spawner CLI:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" incubator/cli.py spawn \
  --path "<absolute target path>" \
  --spec-note-id "<C-YYYY-NN from step 2>" \
  --session-id "<session_id>"
```

The CLI will:
1. Re-validate the path (absolute, doesn't exist, parent exists, not inside another git repo).
2. `git init` the new directory.
3. Drop `.gitignore`, `pyproject.toml` (poetry), and `CLAUDE.md`.
4. Migrate the spec note from apiary's scribe into the new repo's scribe and close the original.

Exit codes from the CLI:
- `0` — success
- `2` — validation error (bad path)
- `3` — spec note not found
- `4` — spawn failure (rolled back automatically)
- `5` — partial success (repo created, migration failed; user must recover manually)

---

## Step 5: Hand off

On success, print the new repo path to the user and suggest next steps:

> Spawned at `<path>`. Open a fresh Claude Code session there to start work. The full spec lives in the new repo's scribe — `scribe/notes.py list --type context` will surface it.

On failure, surface the CLI's stderr verbatim and stop. Do not retry without user direction.

---

## What this skill does NOT do

- **Triage parked ideas.** The user invokes `/incubator` only when they're ready to start; there's no "incubator queue" to manage.
- **Choose the target path.** The user always supplies it.
- **Create a GitHub remote.** Local `git init` only. The user wires up remotes themselves.
- **Track the spawned project after the fact.** Once spawned, the new repo is on its own.
- **Help implement the project.** That's `/refine` + plan mode + normal Claude Code in the new repo's session.
