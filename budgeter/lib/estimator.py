"""Nudge heuristics for the budgeter's PreToolUse hook.

Two of them, measuring two different ceilings:

* ``session_length_nudge`` — how full the model's *context window* is.
* ``usage_limit_nudge`` — how much of the *subscription's* 5-hour and 7-day
  limits are spent, read from the samples the Stop hook records.

Both are pure: they take numbers and config and hand back messages. The hook
owns the I/O, the flag gating and the one-shot sentinels.

This module used to also hold the "is this task going to be expensive"
estimator — keyword/file/step rules, weighted scoring and a
median-of-similar-tasks magnitude estimate. The 2026-08 review measured it
over 3,700 real tasks at 9% precision against a 25% base rate, and it was
deleted along with the feedback log and the ``budgeter-warn`` flag that
gated it. What is left are the nudges that are trivially correct.
"""

from datetime import datetime, timezone
from typing import NamedTuple, Optional

from budgeter.lib.usage_samples import parse_ts

# The windows the nudge covers, each with the label and reset format it reads
# best in. Deliberately not the model-specific `seven_day_opus` /
# `seven_day_sonnet` sub-meters: those move with whichever model you happen
# to be on, and a warning about a meter you are not currently drawing down is
# noise.
USAGE_WARN_WINDOWS = (
    ("five_hour", "5-hour", "%H:%M UTC"),
    ("seven_day", "7-day", "%a %d %b"),
)

DEFAULT_SOFT_PCT = 75
DEFAULT_HARD_PCT = 90
DEFAULT_MAX_SAMPLE_AGE_SECONDS = 1800


class UsageNudge(NamedTuple):
    """One window's warning.

    *resets_at* rides along so the hook can key its one-shot sentinel on it
    and re-arm when the window rolls over.
    """

    window: str  # "five_hour" | "seven_day"
    tier: str  # "soft" | "hard"
    resets_at: str  # the sample's reset timestamp; "" when the endpoint omitted it
    message: str


def session_length_nudge(context_tokens, config):
    """
    Return (tier, message) for the current context-window utilization, or
    (None, None) if we are below the soft threshold.

    *context_tokens* is the size of the most recent prompt (uncached input +
    cache reads + cache writes), which directly corresponds to how full the
    model's context window is. Thresholds are configurable with defaults
    calibrated for the 1M-context Opus model.
    """
    hard = config.get("session_warn_hard_tokens", 800000)
    soft = config.get("session_warn_soft_tokens", 600000)
    if context_tokens >= hard:
        return (
            "hard",
            f"Session context is very long ({context_tokens:,} tokens). "
            "Suggest to the user that they start a new session now — "
            "context-compression fidelity loss is likely beyond this point.",
        )
    if context_tokens >= soft:
        return (
            "soft",
            f"Session context is getting long ({context_tokens:,} tokens). "
            "Consider wrapping up at the next natural checkpoint and "
            "suggesting the user start a fresh session.",
        )
    return (None, None)


def usage_limit_nudge(sample, config, now: Optional[datetime] = None) -> list[UsageNudge]:
    """Return one :class:`UsageNudge` per window that has crossed a threshold.

    *sample* is a record from ``budgeter.lib.usage_samples.latest_sample`` —
    per-window ``{"utilization": float, "resets_at": str}`` plus the parsed
    ``_ts`` of when it was taken. ``None`` or anything malformed yields ``[]``:
    a warning we cannot substantiate is worse than no warning.

    Two staleness guards, because the sample is read from a file rather than
    fetched and can therefore describe a world that no longer exists:

    * a sample older than ``usage_warn_max_sample_age_seconds`` is ignored
      wholesale — nobody has been working, so nothing has moved, and the
      percentage below is from a window that may since have reset;
    * a window whose ``resets_at`` has already passed is skipped individually.
      Its ``utilization`` describes the *previous* window and is usually the
      high-water mark right before the reset, which is exactly the number
      that would produce a confident, wrong warning.
    """
    if not isinstance(sample, dict):
        return []
    now = now or datetime.now(timezone.utc)

    taken_at = sample.get("_ts")
    if not isinstance(taken_at, datetime):
        return []
    max_age = config.get("usage_warn_max_sample_age_seconds", DEFAULT_MAX_SAMPLE_AGE_SECONDS)
    if (now - taken_at).total_seconds() > max_age:
        return []

    nudges = []
    for key, label, reset_fmt in USAGE_WARN_WINDOWS:
        window = sample.get(key)
        if not isinstance(window, dict):
            continue
        util = window.get("utilization")
        if not isinstance(util, (int, float)) or isinstance(util, bool):
            continue

        resets_at = window.get("resets_at") if isinstance(window.get("resets_at"), str) else ""
        reset_dt = parse_ts(resets_at)
        if reset_dt is not None and reset_dt <= now:
            continue
        # A reset time we could not parse still beats none: say nothing about
        # when it lifts rather than guessing.
        when = f" (resets {reset_dt.strftime(reset_fmt)})" if reset_dt is not None else ""

        hard = config.get(f"usage_warn_{key}_hard_pct", DEFAULT_HARD_PCT)
        soft = config.get(f"usage_warn_{key}_soft_pct", DEFAULT_SOFT_PCT)
        if util >= hard:
            tier = "hard"
            message = (
                f"Account usage: {label} limit at {util:.0f}%{when}. "
                "Tell the user before taking on anything further — there is "
                "little headroom left and work in flight may be cut off."
            )
        elif util >= soft:
            tier = "soft"
            message = (
                f"Account usage: {label} limit at {util:.0f}%{when}. "
                "Prefer the cheaper path and hold off on large fan-outs "
                "until it resets."
            )
        else:
            continue
        nudges.append(UsageNudge(key, tier, resets_at, message))
    return nudges
