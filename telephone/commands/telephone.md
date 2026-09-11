---
name: telephone
description: Ask another registered repo a question, or have it do a piece of work, without leaving this session
user-invocable: true
---

# /telephone: call another repo

Starts a headless Claude Code run in another registered repo, with that repo as the working directory, and brings back only its reply. The far repo answers with its own `CLAUDE.md`, hooks and scribe notes, so you never have to read it into this session.

## Usage

```
/telephone <repo> <message>          # answer mode: the callee reads and replies
/telephone <repo> act <message>      # act mode: the callee may change files
```

`<repo>` is the registry name (`apiary doctor registry` lists them) or a path to the checkout.

## Steps

1. Place the call in the background, so this session is re-invoked when the reply lands:

   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" telephone/cli.py call <repo> "<message>"
   ```

   Run it through the Bash tool with `run_in_background: true`. Add `--act` when the user typed `act`. Add `--wait` and run in the foreground only when the user asked to wait for the answer before anything else happens.

2. Read the reply the CLI printed. It carries the answer, anything the callee asked back, any changes it made, and a `[telephone]` line with the call id.

3. Report the answer to the user and name the call id. If the callee asked a question you can answer yourself, send one follow-up:

   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" telephone/cli.py reply <call-id> "<message>"
   ```

4. `telephone/cli.py show <call-id>` prints the whole line, and `list` prints the calls on record.

## Rules you have to follow

- **Name every call you placed on your own.** When you call another repo without the user typing `/telephone`, say so in your reply to the user, with the call id and what you asked. A call costs the user's subscription and they get no other signal that it happened.
- **Act mode is never yours to choose.** It runs only on a grant the `UserPromptSubmit` hook writes when the user types `/telephone <repo> act ...`. Running `call --act` without one exits 1, and so does a follow-up on an act call. If act mode is what the work needs, ask the user to type the command.
- **You get three calls of your own per session.** After that `call` exits 1 and tells you to ask the user. A call the user typed does not count against it.
- **One line holds six autonomous exchanges.** A seventh `reply` without a fresh grant exits 1. Bring the thread to the user instead of trying again.
- **Never call from inside a call.** A callee run has `APIARY_TELEPHONE_CALL` in its environment and `call` refuses there. If the answer needs a third repo, say so and let the caller decide.
- **Do not paste the callee's files into this session.** Only the reply text is meant to cross.

## What the callee sees

A preamble naming the caller, the mode, the call id and the reply format, then the message. In answer mode its tools are read-only. In act mode it works on branch `telephone/<call-id>`, and the CLI checks afterwards that the branch exists, that the checkout came back to its original branch, and that no remote tracking ref moved. A moved ref is recorded as `issue: push detected` on the record and in both repos' notes.

## Where it is written down

Every call becomes a record at `<main-apiary>/.apiary/telephone/<year>/<seq>.md` with the id `C-<year>-<seq>`, and both repos get a scribe `context` note pointing at it.
