"""Shared high-signal secret patterns for both secret-scanning gates.

Two gates scan for credentials, at different moments:

* ``scripts/secret_scan.py`` — commit time, over the staged diff.
* ``core/hooks/pre_push_secret_scan.py`` — push time, over the outgoing diff.

They started with independent regex tables, which is a slow leak: a pattern
added to one silently leaves the other weaker, and nothing fails when they
drift apart (#T-2026-260). The literal-credential rules live here so both
import the same list.

What is deliberately NOT shared: each gate's generic ``key = value`` heuristic.
The push gate accepts an assignment only when the value clears a Shannon
entropy bar; the commit gate filters placeholders, env-var indirection, and
prose. Both are defensible, they are tuned against different false-positive
pressures, and collapsing them would change what a live gate blocks. That is a
behaviour change, not a de-duplication, so it stays out of this module.

Rules here are high-signal by design: a hit should almost always be a real
credential, because a false positive blocks work.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class SecretPattern(NamedTuple):
    """One reviewed detection rule.

    ``name`` appears in both gates' output, so the two report the same finding
    by the same label. ``hint`` is the human-readable gloss the commit gate
    prints; the push gate shows only the name.
    """

    name: str
    regex: re.Pattern[str]
    hint: str


# Ordered most-specific first: `sk-ant-` before the general `sk-` form, so a
# finding is labelled with the narrower rule that matched.
PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
        "a PEM private key block",
    ),
    SecretPattern(
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "an AWS access key id",
    ),
    SecretPattern(
        "anthropic-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}"),
        "an Anthropic API key",
    ),
    SecretPattern(
        "openai-key",
        re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}"),
        "an OpenAI-style API key",
    ),
    SecretPattern(
        "github-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
        "a GitHub token",
    ),
    SecretPattern(
        "slack-token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
        "a Slack token",
    ),
    SecretPattern(
        "google-api-key",
        re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
        "a Google API key",
    ),
    SecretPattern(
        "bearer-token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
        "a bearer token",
    ),
    SecretPattern(
        "basic-auth-url",
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s:@]+@"),
        "credentials embedded in a URL",
    ),
)

# Allowlist markers, honoured by both gates. The push gate shipped first with
# the detect-secrets convention; the commit gate added its own. Each accepts
# both, so a line silenced for one is silenced for the other.
PRAGMA_RE = re.compile(
    r"apiary:\s*allow-secret|pragma:\s*allowlist\s+secret", re.IGNORECASE
)


def find(line: str) -> tuple[str, str] | None:
    """Return ``(rule_name, matched_text)`` for the first hit in *line*, or None.

    Allowlist pragmas are NOT checked here — each gate applies them alongside
    its own path- and file-level exemptions.
    """
    for pattern in PATTERNS:
        m = pattern.regex.search(line)
        if m:
            return pattern.name, m.group(0)
    return None
