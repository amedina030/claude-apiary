#!/usr/bin/env python3
"""PreToolUse hook — block `git push` when the outgoing commits contain secrets.

A companion to ``pre_push_doc_conformer`` (same fire-at-push design): the
written rule "always sweep the diff for secrets before pushing" has the same
failure mode as the leak it guards against — it relies on remembering. So the
sweep lives here instead: deterministic, can't-forget, fired the moment a push
is attempted.

Behaviour:
  - Fires on Bash tool calls that perform a ``git push`` (detection reused
    verbatim from ``pre_push_doc_conformer.command_pushes``).
  - Works out *what* is being pushed: the ref named on the command line
    (``git push origin feature``), every branch for ``--all``/``--mirror``,
    or ``HEAD`` when nothing is named. Honours ``git -C <dir> push``.
  - Scans the added lines of **every outgoing commit individually** (commits
    reachable from the pushed ref but from no ref on the target remote). A
    secret committed and then removed two commits later is still in the
    history being pushed, and a cumulative base..HEAD diff would not show it.
  - Applies the shared rule table in ``core/secret_patterns`` — the same
    literal patterns and the same generic ``key = value`` rule as the
    commit-time gate, so the two can never disagree.
  - A match BLOCKS the push, reporting each offending ``file:line`` and commit
    with the secret redacted (so the gate's own report never re-leaks it).

Override: append ``apiary:allow-secret`` or the detect-secrets convention
``pragma: allowlist secret`` to a line to whitelist an intentional fixture.

Failure policy. Internal errors *before* the scan (bad payload, git not on
PATH) fail open so a buggy gate cannot wedge every push. But once we know a
push is happening, a scan that does not complete — git timing out on a huge
first push, or exiting non-zero — BLOCKS with a message saying the scan did
not run: a security control that quietly stops working is worse than one that
is loudly broken. Runner subprocesses are exempt (consistent with the other
core hooks, #228).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# Make the repo root importable before any cross-import from `core` — this hook
# has top-level `core` imports, so the sys.path setup that other hooks defer
# into their functions must happen here at module scope (stdlib →
# sys.path.insert → internal, per docs/standards/code-style.md).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core import secret_patterns  # noqa: E402

# The push detector is identical to the doc-conformer's; reuse it so the two
# gates can never disagree about what counts as a push.
from core.hooks.pre_push_doc_conformer import command_pushes  # noqa: E402

_ALLOW_PRAGMA = secret_patterns.PRAGMA_RE

# Kept for callers/tests that import it by this name.
_shannon_entropy = secret_patterns.shannon_entropy

_SHA_LINE = re.compile(r"^[0-9a-f]{40}$")
_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\|")
_OPTS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec", "-C", "-c"}
_GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}

SCAN_TIMEOUT_SECONDS = 120


class PushTarget(NamedTuple):
    """What a ``git push`` command line is about to send."""

    remote: str | None      # named remote, or None for "default"/URL
    refs: tuple[str, ...]   # local refs to scan; ("HEAD",) when unnamed
    everything: bool        # --all / --mirror / --branches
    cwd: str | None         # from `git -C <dir>`, if given


def scan_line(content: str):
    """Return a list of ``(rule, redacted_preview)`` secrets found in *content*.

    Pure and fully unit-testable — the file/line bookkeeping lives in
    ``scan_diff``. A line carrying the allowlist pragma yields nothing.
    """
    if not content or _ALLOW_PRAGMA.search(content):
        return []
    hit = secret_patterns.find_any(content)
    if hit is None:
        return []
    return [(hit.rule, hit.prefix + _redact(hit.secret))]


def _redact(secret: str) -> str:
    """Show only enough of *secret* to locate it, never the whole value."""
    secret = secret.strip()
    if len(secret) <= 8:
        return secret[:2] + "***"
    return f"{secret[:4]}…{secret[-2:]} ({len(secret)} chars)"


def _unquote_path(target: str) -> str:
    """Undo git's C-style quoting of unusual paths (``"b/a \\"b\\".py"``)."""
    if len(target) >= 2 and target[0] == '"' and target[-1] == '"':
        inner = target[1:-1]
        return re.sub(r"\\(.)", r"\1", inner)
    return target


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
            target = _unquote_path(raw[4:].strip())
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


def scan_patch_series(log_text: str):
    """Scan ``git log -p --format=%H`` output: one patch per commit.

    Returns ``(sha, path, lineno, rule, redacted_preview)`` tuples, de-duplicated
    on ``(path, rule, preview)`` so a secret carried through several commits is
    reported once, against the first commit that introduced it.
    """
    findings = []
    seen = set()
    sha = ""
    chunk: list[str] = []

    def flush():
        for path, lineno, rule, preview in scan_diff("\n".join(chunk)):
            key = (path, rule, preview)
            if key in seen:
                continue
            seen.add(key)
            findings.append((sha, path, lineno, rule, preview))

    for raw in log_text.splitlines():
        if _SHA_LINE.match(raw):
            flush()
            sha = raw
            chunk = []
            continue
        chunk.append(raw)
    flush()
    return findings


def _looks_like_url(token: str) -> bool:
    return "://" in token or token.startswith("git@") or token.endswith(".git")


def push_target(command: str) -> PushTarget:
    """Parse the push segment of *command* into what will be pushed.

    Handles compound commands (``git add . && git push origin main``), ``-C``,
    ``--all``/``--mirror``, ``+ref``, ``src:dst`` refspecs, ``tag <name>``, and
    deletions (``:ref`` — nothing outgoing). Unknown shapes fall back to HEAD,
    which is the conservative choice: HEAD is what an unqualified push sends.
    """
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            toks = shlex.split(segment, posix=True)
        except ValueError:
            toks = segment.split()
        if "git" not in toks:
            continue
        i = toks.index("git") + 1
        cwd = None
        # git's own global options come before the subcommand.
        while i < len(toks) and toks[i].startswith("-"):
            tok = toks[i]
            if tok in _GIT_GLOBAL_WITH_VALUE and i + 1 < len(toks):
                if tok == "-C":
                    cwd = toks[i + 1]
                i += 2
                continue
            if tok.startswith("-C") and len(tok) > 2:
                cwd = tok[2:]
            i += 1
        if i >= len(toks) or toks[i] != "push":
            continue
        i += 1
        everything = False
        positional: list[str] = []
        while i < len(toks):
            tok = toks[i]
            if tok in ("--all", "--mirror", "--branches"):
                everything = True
            elif tok.startswith("-"):
                if tok in _OPTS_WITH_VALUE and i + 1 < len(toks):
                    i += 1
            else:
                positional.append(tok)
            i += 1
        remote = None
        refs: list[str] = []
        if positional:
            remote = None if _looks_like_url(positional[0]) else positional[0]
            specs = positional[1:]
            j = 0
            while j < len(specs):
                spec = specs[j]
                if spec == "tag" and j + 1 < len(specs):
                    refs.append(specs[j + 1])
                    j += 2
                    continue
                src = spec.lstrip("+").split(":", 1)[0]
                if src:                     # ":ref" is a deletion — nothing outgoing
                    refs.append(src)
                j += 1
        return PushTarget(remote, tuple(refs) or ("HEAD",), everything, cwd)
    return PushTarget(None, ("HEAD",), False, None)


def outgoing_log_args(target: PushTarget, verified_refs: list[str]) -> list[str]:
    """Build the ``git log`` argument list for the outgoing patch series."""
    refs = ["--branches"] if target.everything else (verified_refs or ["HEAD"])
    remotes = f"--remotes={target.remote}" if target.remote else "--remotes"
    return [
        "-c", "core.quotepath=false",
        "log", "-p", "--no-color", "--no-ext-diff", "--no-textconv", "--format=%H",
        *refs, "--not", remotes,
    ]


def main():
    """Entry point. Fail-open only on internal errors *before* the scan."""
    try:
        _run()
    except Exception:
        from core.hook_context import hook_allow
        hook_allow()


def _run():  # pragma: no cover — the pure pieces are tested; this is the shell.
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

    target = push_target(command)
    cwd = payload.get("cwd") or os.getcwd()
    if target.cwd:
        cwd = str((Path(cwd) / target.cwd).resolve())

    def _git(*args, timeout=30):
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )

    # Everything up to here is "is this even a push we can inspect?" — git
    # missing entirely is an internal error and fails open. From the first
    # real git call on, a scan that doesn't complete blocks.
    try:
        verified = [
            r for r in target.refs
            if _git("rev-parse", "--verify", "--quiet", r).returncode == 0
        ]
    except (OSError, subprocess.SubprocessError):
        hook_allow()
        return

    try:
        log = _git(*outgoing_log_args(target, verified), timeout=SCAN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        hook_block(
            f"Push blocked: the secret scan did not finish within "
            f"{SCAN_TIMEOUT_SECONDS}s, so the outgoing commits were NOT checked. "
            "Run `python scripts/secret_scan.py --path .` in the repo, then push "
            "from a terminal if it is clean."
        )
        return
    except (OSError, subprocess.SubprocessError) as exc:
        hook_block(
            "Push blocked: the secret scan could not run git "
            f"({exc.__class__.__name__}), so the outgoing commits were NOT checked."
        )
        return
    if log.returncode != 0:
        hook_block(
            "Push blocked: the secret scan could not list the outgoing commits, "
            "so they were NOT checked.\n\n" + (log.stderr or "").strip()
        )
        return

    findings = scan_patch_series(log.stdout)
    if not findings:
        hook_allow()
        return

    lines = [f"  {path}:{lineno}  @{sha[:8]}  [{rule}]  {preview}"
             for sha, path, lineno, rule, preview in findings]
    hook_block(
        "Push blocked: possible secret(s) in the outgoing commits. The values "
        "below are about to leave your machine — verify each, then remove or "
        "rotate it before pushing. A secret that was later deleted is still in "
        "the history being pushed; rewrite the commits, don't just remove the line.\n\n"
        + "\n".join(lines)
        + "\n\nIf a hit is an intentional fixture/example, append "
        "'apiary:allow-secret' (or 'pragma: allowlist secret') to that line."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
