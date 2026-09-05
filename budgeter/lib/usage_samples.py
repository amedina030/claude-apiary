"""Usage-limit samples: the Claude Code OAuth usage endpoint, logged over time.

The subscription's 5-hour and 7-day limits are the only quantity that
actually matters on this machine (there is no API key), and the endpoint
``core/usage_fetcher.py`` reads is the only ground truth for them. Nothing
else records how those percentages move, so this module appends one compact
sample to ``budgeter/data/usage_samples.jsonl`` whenever a sampler asks and
the previous sample is older than ``usage_sample_interval_seconds``
(``budgeter/config.json``, default 300):

* the budgeter Stop hook (``budgeter/hooks/stop_session.py``) — fires at the
  end of every assistant turn in every apiary repo, interactive or headless,
  which is exactly when the numbers move;
* the GUI's usage poller (``gui/app.py``) — already holds a fresh payload
  every 60 s, so it records instead of fetching.

``budgeter/usage_calibrate.py`` joins the samples to the transcript load so
"one percent of the 7-day limit" gets a measured meaning.

Fail-open everywhere: a sampler must never delay or break a hook. The one
deliberate exception is ``APIARY_BUDGETER_TEST_ISOLATION=1``, under which any
attempt to touch the production samples file raises before a network call can
happen — the same guard ``budgeter.lib.logger`` applies to its log.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.utils.filelock import FileLock

BUDGETER_DIR = Path(__file__).resolve().parent.parent
SAMPLES_PATH = BUDGETER_DIR / "data" / "usage_samples.jsonl"
CONFIG_PATH = BUDGETER_DIR / "config.json"
DEFAULT_INTERVAL_SECONDS = 300
WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")

_DEFAULT_SAMPLES_PATH = SAMPLES_PATH
_TAIL_BYTES = 4096


def _assert_isolated_in_test_mode(path: Path) -> None:
    if os.environ.get("APIARY_BUDGETER_TEST_ISOLATION") != "1":
        return
    if Path(path).resolve() == _DEFAULT_SAMPLES_PATH.resolve():
        raise RuntimeError(
            "budgeter test-isolation violation: usage sampler pointed at the production "
            f"samples file {_DEFAULT_SAMPLES_PATH} while APIARY_BUDGETER_TEST_ISOLATION=1. "
            "Pass path= explicitly in tests."
        )


def sample_interval_seconds(config_path: Optional[Path] = None) -> int:
    """``usage_sample_interval_seconds`` from budgeter/config.json, else the default.

    Always the main apiary config: usage is per account, so the per-project
    redirect ``logger.configure_for_project`` applies to the token log does not
    apply here.
    """
    try:
        data = json.loads(Path(config_path or CONFIG_PATH).read_text(encoding="utf-8"))
        value = int(data.get("usage_sample_interval_seconds", DEFAULT_INTERVAL_SECONDS))
        return value if value > 0 else DEFAULT_INTERVAL_SECONDS
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_INTERVAL_SECONDS


def compact(payload: dict) -> dict:
    """Keep only the per-window ``utilization`` / ``resets_at`` pairs.

    The raw endpoint payload carries a dozen experimental keys; the sample
    file should stay small and stable across endpoint changes.
    """
    out: dict = {}
    for window in WINDOWS:
        value = payload.get(window) if isinstance(payload, dict) else None
        if isinstance(value, dict):
            util = value.get("utilization")
            out[window] = {
                "utilization": float(util) if isinstance(util, (int, float)) else None,
                "resets_at": value.get("resets_at")
                if isinstance(value.get("resets_at"), str)
                else None,
            }
        else:
            out[window] = None
    return out


def parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def last_sample_ts(path: Optional[Path] = None) -> Optional[datetime]:
    """Timestamp of the last well-formed sample, reading only the file's tail."""
    target = Path(path or SAMPLES_PATH)
    try:
        size = target.stat().st_size
        with open(target, "rb") as fh:
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = parse_ts(rec.get("ts")) if isinstance(rec, dict) else None
        if ts is not None:
            return ts
    return None


def is_due(
    min_interval_s: Optional[int] = None,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    """True when no sample exists or the last one is older than the interval."""
    interval = sample_interval_seconds() if min_interval_s is None else int(min_interval_s)
    now = now or datetime.now(timezone.utc)
    last = last_sample_ts(path)
    if last is None:
        return True
    return (now - last).total_seconds() >= interval


def record_sample(
    payload: dict,
    source: str,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """Append one sample; returns the record, or None if it could not be written."""
    target = Path(path or SAMPLES_PATH)
    _assert_isolated_in_test_mode(target)
    now = now or datetime.now(timezone.utc)
    record = {"ts": now.astimezone(timezone.utc).isoformat(), "source": source, **compact(payload)}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(target):
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
    except (OSError, TimeoutError) as exc:
        print(f"[budgeter] usage sample not written: {exc!r}", file=sys.stderr)
        return None
    return record


def record_if_due(
    payload: Optional[dict],
    source: str,
    min_interval_s: Optional[int] = None,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """For callers that already hold a payload (the GUI poller)."""
    target = Path(path or SAMPLES_PATH)
    _assert_isolated_in_test_mode(target)
    if not isinstance(payload, dict) or not is_due(min_interval_s, now, target):
        return None
    return record_sample(payload, source, now, target)


def sample_if_due(
    source: str,
    fetch: Callable[[], Optional[dict]],
    min_interval_s: Optional[int] = None,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """Fetch and record one sample if the interval has elapsed.

    The isolation guard runs before *fetch*, so a hook test can never reach
    the network by accident.
    """
    target = Path(path or SAMPLES_PATH)
    _assert_isolated_in_test_mode(target)
    if not is_due(min_interval_s, now, target):
        return None
    try:
        payload = fetch()
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        print(f"[budgeter] usage fetch failed: {exc!r}", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        return None
    return record_sample(payload, source, now, target)


def iter_samples(path: Optional[Path] = None, since: Optional[datetime] = None) -> Iterator[dict]:
    """Yield well-formed samples in file order, each with a parsed ``_ts``."""
    target = Path(path or SAMPLES_PATH)
    try:
        with open(target, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = parse_ts(rec.get("ts"))
                if ts is None or (since is not None and ts < since):
                    continue
                rec["_ts"] = ts
                yield rec
    except OSError:
        return
