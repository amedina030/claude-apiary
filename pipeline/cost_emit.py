"""Shared helper to emit per-call cost data from pipeline stages.

Each stage script's run_claude wrapper calls emit_usage_xml after the
subprocess returns. The orchestrator (run.py) scrapes <usage> blocks
from stage stderr and sums them per stage.

Format consumed by run.py's parse_usage_fields:
    <usage>
      <total_tokens>N</total_tokens>
      <tool_uses>N</tool_uses>
      <duration_ms>N</duration_ms>
    </usage>
"""
import json
import sys


def emit_usage_xml(envelope_stdout: str) -> None:
    """Parse Claude -p JSON envelope and emit a <usage> XML block to stderr.

    Silent on any failure — cost logging must never break a pipeline stage.
    """
    if not envelope_stdout:
        return
    try:
        envelope = json.loads(envelope_stdout)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(envelope, dict):
        return

    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return

    # Sum all numeric token fields in the usage dict (input, output, cache_*).
    total_tokens = 0
    for v in usage.values():
        if isinstance(v, (int, float)):
            total_tokens += int(v)

    tool_uses = envelope.get("num_turns", 0)
    if not isinstance(tool_uses, int):
        tool_uses = 0

    duration_ms = envelope.get("duration_ms", 0)
    if not isinstance(duration_ms, int):
        duration_ms = 0

    block = (
        "<usage>"
        f"<total_tokens>{total_tokens}</total_tokens>"
        f"<tool_uses>{tool_uses}</tool_uses>"
        f"<duration_ms>{duration_ms}</duration_ms>"
        "</usage>"
    )
    print(block, file=sys.stderr, flush=True)
