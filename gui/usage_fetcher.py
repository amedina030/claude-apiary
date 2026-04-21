"""Fetch claude-code's live 5-hour and 7-day quota utilization via the
undocumented OAuth usage endpoint (L-2026-114).

Stdlib only. Fail-open everywhere: any problem (missing credentials file,
malformed JSON, network error, 429 from the endpoint) returns ``None`` so the
GUI can render "—" instead of crashing the sidebar.

The endpoint shape, as of 2026-04:

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <claudeAiOauth.accessToken>
    anthropic-beta: oauth-2025-04-20

    {
      "five_hour":        {"utilization": 4.0, "resets_at": "<ISO8601>"},
      "seven_day":        {"utilization": 42.0, "resets_at": "<ISO8601>"},
      "seven_day_sonnet": {...} | null,
      "seven_day_opus":   {...} | null,
      "extra_usage": {
         "is_enabled": bool,
         "monthly_limit": <int CENTS>,
         "used_credits": <float CENTS>,
         "utilization": <percent>,
         "currency": "USD"
      },
      ...
    }

``extra_usage.monthly_limit`` / ``used_credits`` are CENTS, not dollars —
divide by 100 at the render layer if displayed.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
TIMEOUT_SECONDS = 5.0


def _read_access_token(path: Path = CREDENTIALS_PATH) -> Optional[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def fetch_usage(credentials_path: Path = CREDENTIALS_PATH) -> Optional[dict]:
    """Return the parsed JSON payload, or None on any failure.

    Always re-reads the credentials file rather than caching the token —
    claude-code refreshes it in the background and we want the current one.
    """
    token = _read_access_token(credentials_path)
    if token is None:
        return None
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA_HEADER,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


class UsagePoller:
    """Background thread that calls ``fetch_usage`` on an interval and
    invokes ``on_update(payload_or_none)`` with each result.

    Pattern matches ``gui.scribe_aggregator.ScribeAggregatorService``: daemon
    thread, stop-event-driven wait between ticks, silent on any exception
    (the callback itself may raise — that's the caller's problem, not ours
    to eat, so the user sees tracebacks if their handler misbehaves).
    """

    def __init__(
        self,
        on_update: Callable[[Optional[dict]], None],
        interval: float = 60.0,
        credentials_path: Path = CREDENTIALS_PATH,
    ) -> None:
        self._on_update = on_update
        self._interval = max(5.0, float(interval))
        self._credentials_path = credentials_path
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="usage-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
        self._thread = None

    def fetch_now(self) -> Optional[dict]:
        """One-shot fetch + push. Used at startup to avoid the first-tick
        delay before any numbers show up in the GUI."""
        payload = fetch_usage(self._credentials_path)
        self._on_update(payload)
        return payload

    def _run(self) -> None:
        # Fire immediately on first tick (start_services already calls
        # fetch_now, but the loop is self-sufficient if someone just calls
        # start() without the priming fetch).
        while not self._stop.is_set():
            try:
                payload = fetch_usage(self._credentials_path)
            except Exception:
                payload = None
            try:
                self._on_update(payload)
            except Exception:
                # Never let a broken callback kill the poll loop.
                pass
            if self._stop.wait(self._interval):
                return
