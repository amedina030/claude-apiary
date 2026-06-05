<!-- apiary-context-rules-start -->

<!-- apiary-context-rule:load_apiary_context hash=549a3ad693c4b267b66ab5f3469ff4c563d9a9cabb2501218599e082cdcc4f1a -->
### Load apiary context at session start

At the start of every session, invoke the `/apiary-context` skill to load the apiary toolkit rules (scribe, budgeter, runner conventions, portability, etc.). Do this before any other work.

If `/apiary-context` is not available (e.g. skills not installed), proceed without it — the session is still usable, just without apiary-specific guidance.
<!-- /apiary-context-rule:load_apiary_context -->

<!-- apiary-context-rules-end -->
