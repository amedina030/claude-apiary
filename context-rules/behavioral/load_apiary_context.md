---
id: load_apiary_context
title: Load apiary context at session start
category: behavioral
requires: []
---
### Load apiary context at session start

At the start of every session, invoke the `/apiary-context` skill to load the apiary toolkit rules (scribe, budgeter, runner conventions, portability, etc.). Do this before any other work.

If `/apiary-context` is not available (e.g. skills not installed), proceed without it — the session is still usable, just without apiary-specific guidance.
