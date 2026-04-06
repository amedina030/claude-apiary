"""Shared helper to emit per-call cost data from pipeline stages.

Each stage script's run_claude wrapper calls emit_usage_xml after the
subprocess returns. The orchestrator (run.py) scrapes <usage> blocks
from stage stderr and sums them per stage.

Format consumed by run.py's parse_usage_fields and budgeter/log_agent_cost.py:
    <usage>
      <input_tokens>N</input_tokens>
      <cache_read_input_tokens>N</cache_read_input_tokens>
      <cache_creation_input_tokens>N</cache_creation_input_tokens>
      <output_tokens>N</output_tokens>
      <total_tokens>N</total_tokens>
      <tool_uses>N</tool_uses>
      <duration_ms>N</duration_ms>
    </usage>

The per-category fields are extracted by exact name from the Anthropic
API's usage dict so downstream reporting (budgeter/report.py --weighted)
can apply cost weights to cache reads (0.1x) vs. fresh input (1x) vs.
output (5x). total_tokens is retained for backward compatibility and for
non-weighted displays.
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

    def _int_field(name: str) -> int:
        v = usage.get(name, 0)
        return int(v) if isinstance(v, (int, float)) else 0

    input_tokens = _int_field("input_tokens")
    cache_read = _int_field("cache_read_input_tokens")
    cache_create = _int_field("cache_creation_input_tokens")
    output_tokens = _int_field("output_tokens")

    # total_tokens sums every numeric field in the usage dict so any future
    # categories Anthropic adds are still counted. Per-category fields above
    # are the authoritative source for weighted reporting.
    total_tokens = sum(
        int(v) for v in usage.values() if isinstance(v, (int, float))
    )

    tool_uses = envelope.get("num_turns", 0)
    if not isinstance(tool_uses, int):
        tool_uses = 0

    duration_ms = envelope.get("duration_ms", 0)
    if not isinstance(duration_ms, int):
        duration_ms = 0

    block = (
        "<usage>"
        f"<input_tokens>{input_tokens}</input_tokens>"
        f"<cache_read_input_tokens>{cache_read}</cache_read_input_tokens>"
        f"<cache_creation_input_tokens>{cache_create}</cache_creation_input_tokens>"
        f"<output_tokens>{output_tokens}</output_tokens>"
        f"<total_tokens>{total_tokens}</total_tokens>"
        f"<tool_uses>{tool_uses}</tool_uses>"
        f"<duration_ms>{duration_ms}</duration_ms>"
        "</usage>"
    )
    print(block, file=sys.stderr, flush=True)
