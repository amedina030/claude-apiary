"""Query helpers for the budgeter usage log."""

from budgeter.lib import logger


def total_tokens_for_request(request_id: str) -> int:
    """Return the total tokens_delta for all log entries matching request_id."""
    if not request_id or not isinstance(request_id, str):
        raise ValueError(f"request_id must be a non-empty string, got {request_id!r}")
    # Raise explicitly when the log is absent so callers see an error rather than
    # a silent 0, which is indistinguishable from "no matching entries found".
    # logger.read_log() returns [] on a missing file (common convention), so we
    # must stat it here to guarantee the error branch in harden.md Step 2g/2j fires.
    if not logger.LOG_PATH.exists():
        raise FileNotFoundError(f"Usage log not found: {logger.LOG_PATH}")
    entries = logger.read_log()
    # tokens_delta is used here (not net_tokens_delta): for Agent entries both fields
    # are equal, but tokens_delta is the canonical gross-cost field that report.py
    # also sums — keeping them consistent prevents budget-check / report disagreements.
    return sum(
        int(e.get("tokens_delta", 0) or 0) for e in entries if e.get("request_id") == request_id
    )
