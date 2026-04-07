# Overnight Pipeline Cron Setup

This file documents how to register the overnight remote trigger. Actual registration is a manual step — run the `/schedule` skill in Claude Code when ready.

## Command

```
python pipeline/run.py --detached
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
- **Command:** `python pipeline/run.py --detached`
- **Cron:** `0 2 * * *` (or your preferred expression above)
- **Working directory:** repo root (Claude Code sets this automatically)

## Guardrail knobs

Edit `pipeline/config.json` to tune limits before registering:

| Key | Default | Effect |
|-----|---------|--------|
| `token_cap` | 50000 | Max tokens Claude may spend per detached run |
| `max_unreviewed` | 3 | Blocks new runs when this many pipeline branches await review |

## Morning review

After overnight runs complete, inspect pending branches:

```
python pipeline/queue.py
```

This lists all `pipeline/*` branches joined with `overnight.jsonl` entries so you can decide what to merge or drop.
