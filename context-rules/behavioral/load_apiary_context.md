---
id: load_apiary_context
title: Apiary toolkit — recovery pointer
category: behavioral
requires: []
---
### Apiary context

This repo uses the apiary toolkit. Its rules (launcher convention, scribe/budgeter/runner, portability, compass profile) normally load automatically at session start via the `startup_prompt_hook` — you don't need to invoke anything.

If that context is **missing** (the hook didn't run — e.g. a fresh/untrusted clone, a broken launcher, or a surface that doesn't execute hooks), run the `/apiary-context` skill to load it manually.
