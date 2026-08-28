"""Session-length heuristics for the budgeter's PreToolUse hook.

This module used to also hold the "is this task going to be expensive"
estimator — keyword/file/step rules, weighted scoring and a
median-of-similar-tasks magnitude estimate. The 2026-08 review measured it
over 3,700 real tasks at 9% precision against a 25% base rate, and it was
deleted along with the feedback log and the ``budgeter-warn`` flag that
gated it. What is left is the one nudge that is trivially correct.
"""


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
