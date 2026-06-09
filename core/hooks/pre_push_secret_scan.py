#!/usr/bin/env python3
"""PreToolUse hook — block `git push` when the outgoing diff contains secrets.

A companion to ``pre_push_doc_conformer`` (same fire-at-push design): the
written rule "always sweep the diff for secrets before pushing" has the same
failure mode as the leak it guards against — it relies on remembering. So the
sweep lives here instead: deterministic, can't-forget, fired the moment a push
is attempted.

Behaviour:
  - Fires on Bash tool calls that perform a ``git push`` (detection reused
    verbatim from ``pre_push_doc_conformer.command_pushes``).
  - Computes the *outgoing* diff — the added lines in commits that are on
    ``HEAD`` but not on any remote — and scans those added lines for
    high-signal secret patterns (API keys, private keys, bearer tokens,
    high-entropy ``client_secret``/``password``-style assignments, …).
  - A match BLOCKS the push, reporting each offending ``file:line`` with the
    secret value redacted (so the gate's own report never re-leaks it).

Override: append the detect-secrets convention ``pragma: allowlist secret``
to a line to whitelist an intentional fixture/example. The marker is matched
case-insensitively anywhere on the line.

Fail-open on any *internal* error (bad payload, git not runnable, timeout):
a buggy gate must never wedge your ability to push. Unlike the doc-conformer,
this gate has no per-repo file guard — secret hygiene is universal, so it runs
in every bootstrapped repo. Runner subprocesses are exempt (consistent with
the other core hooks, #228).
"""
from __future__ import annotations

import math
import re

# The push detector is identical to the doc-conformer's; reuse it so the two
# gates can never disagree about what counts as a push.
from core.hooks.pre_push_doc_conformer import command_pushes

# Git's well-known empty-tree object — used as the diff base when the oldest
# outgoing commit is a root commit (no parent to diff against).
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# detect-secrets-style inline allowlist marker. A line carrying this is an
# intentional fixture/example and is skipped.
_ALLOW_PRAGMA = re.compile(r"pragma:\s*allowlist\s+secret", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Secret patterns. Each entry is (rule-name, compiled-regex). They are
# deliberately high-signal: a hit should almost always be a real credential,
# because a false positive blocks a push. Lower-signal "looks secret-ish"
# matching is handled separately by the entropy gate below.
# ---------------------------------------------------------------------------
_PATTERNS = [
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("openai-anthropic-key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private-key-block",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("bearer-token",
     re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
]

# Generic ``name = "value"`` / ``name: value`` assignments whose *name* names a
# credential. These fire only when the value also clears the entropy gate, so
# placeholders like ``password = "changeme"`` or ``api_key: your_key_here`` are
# ignored while real high-entropy secrets are caught.
_ASSIGNMENT = re.compile(
    r"(?i)\b(client_secret|aws_secret_access_key|secret_key|api[_-]?key|"
    r"access_token|auth_token|password|passwd|token)\b\s*[:=]\s*"
    r"['\"]?([^\s'\"]{12,})['\"]?"
)
_ENTROPY_MIN = 4.0  # shannon bits/char; placeholder words fall well below this.


def _shannon_entropy(s: str) -> float:
    """Shannon entropy (bits per character) of *s*. 0.0 for empty input."""
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def scan_line(content: str):
    """Return a list of ``(rule, redacted_preview)`` secrets found in *content*.

    Pure and fully unit-testable — the file/line bookkeeping lives in
    ``scan_diff``. A line carrying the allowlist pragma yields nothing.
    """
    if not content or _ALLOW_PRAGMA.search(content):
        return []
    hits = []
    for rule, pat in _PATTERNS:
        m = pat.search(content)
        if m:
            hits.append((rule, _redact(m.group(0))))
    m = _ASSIGNMENT.search(content)
    if m and _shannon_entropy(m.group(2)) >= _ENTROPY_MIN:
        hits.append(("high-entropy-assignment", _redact(m.group(2))))
    return hits


def _redact(secret: str) -> str:
    """Show only enough of *secret* to locate it, never the whole value."""
    secret = secret.strip()
    if len(secret) <= 8:
        return secret[:2] + "***"
    return f"{secret[:4]}…{secret[-2:]} ({len(secret)} chars)"


def iter_added_lines(diff_text: str):
    """Yield ``(path, new_lineno, content)`` for every added line in a unified
    diff. ``content`` excludes the leading ``+``. Hunk headers advance the
    new-file line counter so reported line numbers match the pushed file.
    """
    path = None
    new_lineno = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            # "+++ b/path/to/file" — strip the b/ prefix; "/dev/null" on delete.
            target = raw[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            path = None if target == "/dev/null" else target
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_lineno = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+"):
            yield path, new_lineno, raw[1:]
            new_lineno += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            # Removed line / "\ No newline" marker — does not consume a new
            # line number, and a removed secret isn't being introduced.
            continue
        else:
            # Context line (leading space, or blank) — advances the counter.
            new_lineno += 1


def scan_diff(diff_text: str):
    """Scan a unified diff's added lines. Return a list of
    ``(path, lineno, rule, redacted_preview)`` findings — empty if clean.
    """
    findings = []
    for path, lineno, content in iter_added_lines(diff_text):
        for rule, preview in scan_line(content):
            findings.append((path or "<unknown>", lineno, rule, preview))
    return findings


def main():
    """Entry point. Fail-open on any internal error — never wedge a push."""
    try:
        _run()
    except Exception:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from core.hook_context import hook_allow
        hook_allow()


def _run():  # pragma: no cover — pure logic lives in scan_diff (tested);
    #                               this is the I/O + subprocess shell.
    import os
    import subprocess
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.hook_context import hook_allow, hook_block, read_payload

    if os.environ.get("APIARY_RUNNER_SUBPROCESS") == "1":
        hook_allow()
        return

    payload = read_payload()
    if (payload.get("tool_name") or "") != "Bash":
        hook_allow()
        return

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if not command or not command_pushes(command):
        hook_allow()
        return

    cwd = payload.get("cwd") or os.getcwd()

    def _git(*args, timeout=30):
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    # Outgoing commits: on HEAD, on no remote. Empty → nothing new to leak.
    try:
        rev = _git("rev-list", "HEAD", "--not", "--remotes")
    except (OSError, subprocess.SubprocessError):
        hook_allow()
        return
    if rev.returncode != 0:
        hook_allow()
        return
    shas = rev.stdout.split()
    if not shas:
        hook_allow()
        return

    # Diff base = parent of the oldest outgoing commit, or the empty tree if it
    # is a root commit. Gives the cumulative added lines about to be pushed.
    oldest = shas[-1]
    try:
        parent = _git("rev-parse", "--verify", "--quiet", f"{oldest}^")
        base = parent.stdout.strip() if parent.returncode == 0 else _EMPTY_TREE
        diff = _git("diff", "--no-color", base, "HEAD", timeout=60)
    except (OSError, subprocess.SubprocessError):
        hook_allow()
        return
    if diff.returncode != 0:
        hook_allow()
        return

    findings = scan_diff(diff.stdout)
    if not findings:
        hook_allow()
        return

    lines = [f"  {path}:{lineno}  [{rule}]  {preview}"
             for path, lineno, rule, preview in findings]
    hook_block(
        "Push blocked: possible secret(s) in the outgoing diff. The values "
        "below are about to leave your machine — verify each, then remove or "
        "rotate it before pushing.\n\n"
        + "\n".join(lines)
        + "\n\nIf a hit is an intentional fixture/example, append "
        "'pragma: allowlist secret' to that line to whitelist it. The gate "
        "fails open on internal errors — only real pattern matches block."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
