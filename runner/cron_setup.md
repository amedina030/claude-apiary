# Overnight Runner Cron Setup

This file documents how to register the overnight remote trigger. Actual registration is a manual step — run the `/schedule` skill in Claude Code when ready.

## Command

```
python runner/run.py --detached
```

## Recommended cron expression

```
0 2 * * *
```

Runs at 2 AM daily. For a 1–5 AM window (spread across hours), use:

```
0 1-5 * * *
```

## Registering the trigger

Open Claude Code and run:

```
/schedule
```

When prompted, supply:
- **Command:** `python runner/run.py --detached`
- **Cron:** `0 2 * * *` (or your preferred expression above)
- **Working directory:** repo root (Claude Code sets this automatically)

## Guardrail knobs

Edit `runner/config.json` to tune limits before registering:

| Key | Default | Effect |
|-----|---------|--------|
| `token_cap` | 2000000 | Max tokens Claude may spend per detached run |
| `max_unreviewed` | 5 | Blocks new runs when this many runner branches await review |

## Morning review

After overnight runs complete, inspect pending branches:

```
python runner/queue.py
```

This lists all `runner/*` branches joined with `overnight.jsonl` entries so you can decide what to merge or drop.
