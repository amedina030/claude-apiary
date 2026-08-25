#!/usr/bin/env python3
"""Commit-time secret scanner (stdlib only, no external binaries).

Reads the *staged* diff — not the working tree — so it checks exactly what
is about to be committed, and reports file, line, and which pattern matched.
Non-zero exit blocks the commit when wired up as a ``pre-commit`` hook.

Zero-dependency by design. ``gitleaks`` and friends need a per-machine
binary install, which breaks the portability contract in PORTABILITY.md, so
the default scanner is plain Python and an external tool is never required.

Sibling gate: ``core/hooks/pre_push_secret_scan.py`` scans the *outgoing*
diff when Claude runs ``git push``. The two are complementary, not redundant.
That one is a Claude Code PreToolUse hook, so it never fires for a commit made
by hand in a terminal, and never fires at all in a repo with no remote — which
is exactly the case that motivated this one (a personal-data repo that is
never pushed has no push to intercept). This gate blocks earlier, at commit,
regardless of who is driving.

Both honour the same allowlist pragmas, so a line exempted for one is exempted
for the other; disagreeing gates would be worse than either alone.

Usage::

    python scripts/secret_scan.py --staged        # what a commit would add
    python scripts/secret_scan.py --path some/dir # ad-hoc scan of a tree
    python scripts/secret_scan.py --staged --entropy   # + high-entropy strings

Escape hatches, in order of preference:

1. ``# apiary:allow-secret`` on the offending line (any comment syntax — the
   pragma is matched as a substring, so ``// apiary:allow-secret`` works too).
2. A repo-root ``.secretsallow`` file: one regex per line, tested against both
   the repo-relative path and the offending line. ``#`` starts a comment.
3. ``git commit --no-verify`` skips every pre-commit hook. The hook prints
   this as a reminder on failure so nobody is stuck.

Exit codes::
    0  clean
    1  findings (or a blocked filename)
    2  bad arguments / not a git repo
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import secret_patterns  # noqa: E402

ALLOW_PRAGMA = "apiary:allow-secret"
ALLOWLIST_FILENAME = ".secretsallow"

# The push-time gate (core/hooks/pre_push_secret_scan.py) predates this one and
# uses the detect-secrets convention. Honour both spellings so a line silenced
# for one gate isn't re-flagged by the other.
_PRAGMA_RE = secret_patterns.PRAGMA_RE

# Values that look like a secret assignment but are obviously not one. Kept
# separate from the patterns so the generic rule can stay broad without
# drowning real findings in noise.
_PLACEHOLDER = re.compile(
    r"""^(?:
          x{3,}                     # xxx, xxxxxx
        | y{3,}                     # yyy
        | \.{3,}                    # ...
        | changeme | placeholder | example | redacted | dummy | sample
        | your[-_a-z0-9]*           # your-api-key-here
        | some[-_a-z0-9]*
        | fake[-_a-z0-9]*
        | test[-_a-z0-9]*
        | none | null | true | false | undefined
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

# A value that is *read* rather than *written*: env lookups, interpolation,
# function calls. These are the correct way to handle a secret, so flagging
# them would punish the right behaviour.
_INDIRECTION = re.compile(
    r"""(
          \$\{?[A-Za-z_]          # ${VAR} / $VAR
        | %\(?[A-Za-z_]           # %(VAR)s / %VAR%
        | \{\{                    # {{ template }}
        | \{[A-Za-z_]             # {var} f-string / format
        | \bos\.environ | \bgetenv | \bos\.getenv
        | \bprocess\.env
        | \bsecrets?_?manager
        | \binput\s*\(
        | \w+\s*\(                # any function call
        | \bimport\b
    )""",
    re.VERBOSE | re.IGNORECASE,
)

# The generic assignment rule's value: a literal that is long enough to be a
# real credential. Quoted or bare, but not a call and not interpolation.
_GENERIC_ASSIGN = re.compile(
    r"""(?P<key>\b(?:api[-_]?key|secret|token|password|passwd|pwd|access[-_]?key
        |auth[-_]?token|client[-_]?secret|private[-_]?token)\b)
        \s*[:=]\s*
        (?P<quote>["']?)
        (?P<value>[A-Za-z0-9_\-./+=]{8,})
        (?P=quote)""",
    re.VERBOSE | re.IGNORECASE,
)


@dataclass(frozen=True)
class Pattern:
    """One reviewed detection rule. ``name`` shows up in the failure output."""

    name: str
    regex: re.Pattern[str]
    hint: str

    def search(self, line: str) -> str | None:
        """Return the matched text, or None. Overridden by generic rules."""
        m = self.regex.search(line)
        return m.group(0) if m else None


def _looks_like_a_credential(value: str) -> bool:
    """Does an UNQUOTED value carry any signal beyond being a long word?

    Prose keeps tripping the generic rule — "# password = whatever you set in
    the dashboard" parses as an assignment of the word "whatever". A real bare
    credential almost always has a digit, mixed case, or punctuation; an
    English word has none of the three. Quoted values skip this check, because
    ``password = "supersecret"`` is an explicit literal no matter how wordy it
    looks.
    """
    return (
        any(c.isdigit() for c in value)
        or (any(c.isupper() for c in value) and any(c.islower() for c in value))
        or any(c in "-_./+=" for c in value)
    )


class _GenericAssignPattern(Pattern):
    """``key = <literal>`` with placeholder, indirection, and prose filtering."""

    def search(self, line: str) -> str | None:
        for m in _GENERIC_ASSIGN.finditer(line):
            value = m.group("value")
            if _PLACEHOLDER.match(value):
                continue
            # Only consider indirection to the RIGHT of the key: a call on the
            # left (`self.get_config()["password"] = ...`) doesn't make the
            # right-hand side safe.
            if _INDIRECTION.search(line[m.start("value"):]):
                continue
            if value.isdigit():          # port numbers, timeouts, ids
                continue
            if not m.group("quote") and not _looks_like_a_credential(value):
                continue                 # bare word in prose, not a secret
            return m.group(0)
        return None


# Literal-credential rules come from the table shared with the push-time gate
# (core/secret_patterns.py) so the two cannot drift apart. The generic
# assignment rule is appended locally: its filtering is specific to this gate,
# and the push gate uses an entropy bar instead — see that module's docstring.
PATTERNS: tuple[Pattern, ...] = tuple(
    Pattern(name=p.name, regex=p.regex, hint=p.hint) for p in secret_patterns.PATTERNS
) + (
    _GenericAssignPattern(
        name="generic-assignment",
        regex=_GENERIC_ASSIGN,
        hint="a credential-looking assignment",
    ),
)

# Filenames that should never be committed, even if .gitignore is bypassed
# with `git add -f`. Checked against the repo-relative staged path.
BLOCKED_FILENAMES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$|sample$|template$|dist$)[^/]+$"),
    re.compile(r"(^|/)id_(?:rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"\.(?:pem|pfx|p12|keystore|jks)$"),
    re.compile(r"(^|/)\.aws/credentials$"),
    re.compile(r"(^|/)\.npmrc$"),
    re.compile(r"(^|/)\.pypirc$"),
)

# Files whose contents are never scanned for line-level secrets. Lockfiles and
# minified bundles are dense pseudo-random text: all noise, no signal.
SKIP_CONTENT: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)(?:poetry|package|yarn|pnpm|Cargo|composer)[-.]lock$"),
    re.compile(r"(^|/)package-lock\.json$"),
    re.compile(r"\.min\.(?:js|css)$"),
    re.compile(r"\.(?:png|jpe?g|gif|ico|pdf|zip|gz|whl|exe|dll|so|dylib)$"),
)

ENTROPY_MIN_LEN = 24
ENTROPY_THRESHOLD = 4.2
_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{%d,}" % ENTROPY_MIN_LEN)


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    pattern: str
    hint: str
    excerpt: str

    def render(self) -> str:
        where = f"{self.path}:{self.line_no}"
        return f"  {where}\n      {self.pattern}: {self.hint}\n      {self.excerpt}"


def _redact(text: str, limit: int = 100) -> str:
    """Shorten a match so the failure message doesn't reprint the whole secret.

    Enough of the head to recognise which line is meant, never the tail.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _git(args: Sequence[str], cwd: Path) -> str:
    """Run a git command and return stdout, or "" when git fails."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, ValueError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def repo_root(start: Path) -> Path | None:
    out = _git(["rev-parse", "--show-toplevel"], start).strip()
    return Path(out) if out else None


def parse_staged_diff(diff: str) -> list[tuple[str, int, str]]:
    """Return ``(path, line_no, text)`` for every added line in a unified diff.

    Only additions are considered: a line that already exists in HEAD is not
    something this commit is introducing. Line numbers come from the hunk
    header and advance per added line, so they point at the post-commit file.
    """
    added: list[tuple[str, int, str]] = []
    path = ""
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            # "+++ b/some/path" — or /dev/null for a deletion.  # noqa: null-device
            # git emits the literal string below; it is diff syntax, not a path
            # we open, so the portable-devnull rule doesn't apply.
            path = "" if target == "/dev/null" else target[2:] if target.startswith("b/") else target  # noqa: null-device
            continue
        if raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            line_no = int(m.group(1)) if m else 0
            continue
        if not path:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            added.append((path, line_no, raw[1:]))
            line_no += 1
        elif raw.startswith(" "):
            line_no += 1
        # '-' lines and everything else don't advance the post-image counter.
    return added


def load_allowlist(root: Path) -> list[re.Pattern[str]]:
    """Compile ``.secretsallow`` into regexes. Unreadable/invalid lines are skipped."""
    path = root / ALLOWLIST_FILENAME
    if not path.is_file():
        return []
    out: list[re.Pattern[str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw in text.splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        try:
            out.append(re.compile(entry))
        except re.error:
            print(f"{ALLOWLIST_FILENAME}: skipping invalid regex: {entry}", file=sys.stderr)
    return out


def _allowed(path: str, line: str, allowlist: Iterable[re.Pattern[str]]) -> bool:
    if _PRAGMA_RE.search(line):
        return True
    return any(rx.search(path) or rx.search(line) for rx in allowlist)


def _matches(path: str) -> bool:
    return any(rx.search(path) for rx in SKIP_CONTENT)


def scan_lines(
    lines: Iterable[tuple[str, int, str]],
    allowlist: Iterable[re.Pattern[str]] = (),
    entropy: bool = False,
) -> list[Finding]:
    """Apply every pattern to each ``(path, line_no, text)`` triple."""
    allowlist = list(allowlist)
    findings: list[Finding] = []
    for path, line_no, text in lines:
        if _matches(path):
            continue
        if _allowed(path, text, allowlist):
            continue
        for pattern in PATTERNS:
            hit = pattern.search(text)
            if hit:
                findings.append(
                    Finding(path, line_no, pattern.name, pattern.hint, _redact(hit))
                )
                break            # one finding per line is enough to block
        else:
            if entropy:
                for token in _ENTROPY_TOKEN.findall(text):
                    if shannon_entropy(token) >= ENTROPY_THRESHOLD:
                        findings.append(
                            Finding(
                                path,
                                line_no,
                                "high-entropy",
                                f"a random-looking string (entropy >= {ENTROPY_THRESHOLD})",
                                _redact(token),
                            )
                        )
                        break
    return findings


def blocked_files(paths: Iterable[str], allowlist: Iterable[re.Pattern[str]] = ()) -> list[Finding]:
    """Findings for staged paths that should never be committed at all."""
    allowlist = list(allowlist)
    out: list[Finding] = []
    for path in paths:
        if not path:
            continue
        if any(rx.search(path) for rx in allowlist):
            continue
        for rx in BLOCKED_FILENAMES:
            if rx.search(path):
                out.append(
                    Finding(
                        path,
                        0,
                        "blocked-file",
                        "this filename holds credentials by convention",
                        path,
                    )
                )
                break
    return out


def staged_paths(root: Path) -> list[str]:
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"], root)
    return [line.strip() for line in out.splitlines() if line.strip()]


def scan_staged(root: Path, entropy: bool = False) -> list[Finding]:
    allowlist = load_allowlist(root)
    diff = _git(["diff", "--cached", "--unified=0", "--no-color"], root)
    findings = blocked_files(staged_paths(root), allowlist)
    findings.extend(scan_lines(parse_staged_diff(diff), allowlist, entropy))
    return findings


def scan_path(target: Path, root: Path | None = None, entropy: bool = False) -> list[Finding]:
    """Scan every readable text file under ``target`` (ad-hoc, not commit-time)."""
    base = root or target
    allowlist = load_allowlist(base)
    triples: list[tuple[str, int, str]] = []
    paths: list[str] = []
    files = sorted(target.rglob("*")) if target.is_dir() else [target]
    for f in files:
        if not f.is_file():
            continue
        if any(part == ".git" for part in f.parts):
            continue
        try:
            rel = f.relative_to(base).as_posix()
        except ValueError:
            rel = f.as_posix()
        paths.append(rel)
        if _matches(rel):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue        # binary or unreadable: nothing to scan
        for i, line in enumerate(text.splitlines(), start=1):
            triples.append((rel, i, line))
    findings = blocked_files(paths, allowlist)
    findings.extend(scan_lines(triples, allowlist, entropy))
    return findings


def report(findings: Sequence[Finding], *, hook_mode: bool) -> None:
    n = len(findings)
    plural = "" if n == 1 else "s"
    verdict = " — commit blocked" if hook_mode else ""
    print(f"\nsecret-scan: {n} finding{plural}{verdict}\n", file=sys.stderr)
    for f in findings:
        print(f.render(), file=sys.stderr)
    print("", file=sys.stderr)
    print("If a finding is a false positive, either:", file=sys.stderr)
    print(f"  - add '{ALLOW_PRAGMA}' as a comment on that line, or", file=sys.stderr)
    print(f"  - add a regex for it to {ALLOWLIST_FILENAME} in the repo root.", file=sys.stderr)
    if hook_mode:
        print("  - last resort: git commit --no-verify (skips ALL pre-commit hooks).", file=sys.stderr)
    print("", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true", help="Scan the staged diff (pre-commit).")
    group.add_argument("--path", type=Path, help="Scan a file or directory tree instead.")
    parser.add_argument(
        "--entropy",
        action="store_true",
        help="Also flag high-entropy strings. Noisier; off by default.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print nothing on a clean scan (the pre-commit hook's default).",
    )
    args = parser.parse_args(argv)

    if args.staged:
        root = repo_root(Path.cwd())
        if root is None:
            print("secret-scan: not inside a git repository", file=sys.stderr)
            return 2
        findings = scan_staged(root, entropy=args.entropy)
    else:
        target = args.path.expanduser()
        if not target.exists():
            print(f"secret-scan: no such path: {target}", file=sys.stderr)
            return 2
        findings = scan_path(target, repo_root(target if target.is_dir() else target.parent), args.entropy)

    if findings:
        report(findings, hook_mode=args.staged)
        return 1
    if not args.quiet:
        print("secret-scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
