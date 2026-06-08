## Installing or updating this repo

If you're asked to set up or update claude-apiary on a machine, don't run the
steps by hand — use the one-command scripts. They handle Python discovery, the
Windows Store python-alias trap, Poetry, the bootstrap chain, and the desktop GUI.

- **First install:** `scripts\install.ps1` (Windows) or `scripts/install.sh`
  (macOS/Linux). Add `-Gui` / `--gui` for the desktop app.
- **Update:** `scripts\update.ps1` / `scripts/update.sh` (same flags) — pulls
  latest and re-syncs everything.

The user runs these (`! .\scripts\install.ps1`), since `self-bootstrap` is
blocked for an agent on a fresh clone. Full details: [SETUP.md](SETUP.md).

<!-- apiary-context-rules-start -->

<!-- apiary-context-rule:load_apiary_context hash=61c8d65a2fb3a78752f3977d97a598f4c0fc5fdb95fcb40bdab34474add29c6c -->
### Apiary context

This repo uses the apiary toolkit. Its rules (launcher convention, scribe/budgeter/runner, portability, compass profile) normally load automatically at session start via the `startup_prompt_hook` — you don't need to invoke anything.

If that context is **missing** (the hook didn't run — e.g. a fresh/untrusted clone, a broken launcher, or a surface that doesn't execute hooks), run the `/apiary-context` skill to load it manually.
<!-- /apiary-context-rule:load_apiary_context -->

<!-- apiary-context-rules-end -->
