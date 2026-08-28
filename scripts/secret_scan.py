#!/usr/bin/env python3
"""Commit-time secret scanner (stdlib only, no external binaries).

Reads the *staged* diff — not the working tree — so it checks exactly what
is about to be committed, and reports file, line, and which pattern matched.
Non-zero exit blocks the commit when wired up as a ``pre-commit`` hook.

Zero-dependency by design: git hooks resolve ``py -3`` / ``python3`` /
``python``, not the Poetry virtualenv, so the scanner can only import the
standard library. ``gitleaks`` and friends also need a per-machine binary,
which breaks the portability contract in PORTABILITY.md.

Sibling gate: ``core/hooks/pre_push_secret_scan.py`` scans the *outgoing*
commits when Claude runs ``git push``. The two are complementary, not
redundant. That one is a Claude Code PreToolUse hook, so it never fires for a
commit made by hand in a terminal, and never fires at all in a repo with no
remote — which is exactly the case that motivated this one (a personal-data
repo that is never pushed has no push to intercept). This gate blocks earlier,
at commit, regardless of who is driving.

Both gates apply the same rules (``core/secret_patterns``) and honour the same
allowlist pragmas, so a line exempted for one is exempted for the other;
disagreeing gates would be worse than either alone.

Usage::

    python scripts/secret_scan.py --staged        # what a commit would add
    python scripts/secret_scan.py --path some/dir # ad-hoc scan of a tree
    python scripts/secret_scan.py --staged --entropy   # + high-entropy strings

Escape hatches, in order of preference:

1. ``# apiary:allow-secret`` on the offending line (any comment syntax — the
   pragma is matched as a substring, so ``// apiary:allow-secret`` works too).
2. A repo-root ``.secretsallow`` file: one regex per line. A plain entry is
   matched against the repo-relative *path* and exempts that whole file; an
   entry prefixed ``line:`` is matched against the offending *line* instead.
   ``#`` starts a comment.
3. ``git commit --no-verify`` skips every pre-commit hook. The hook prints
   this as a reminder on failure so nobody is stuck.

Fails closed: if git itself cannot be run (missing binary, locked index, a
corrupt repo) the scan reports that it did NOT run and exits 2, which blocks
the commit. A security control that quietly stops working is worse than one
that is loudly broken.

Exit codes::
    0  clean
    1  findings (or a blocked filename)
    2  bad arguments / not a git repo / the scan could not run
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import secret_patterns  # noqa: E402

ALLOW_PRAGMA = "apiary:allow-secret"
ALLOWLIST_FILENAME = secret_patterns.ALLOWLIST_FILENAME
ALLOWLIST_LINE_PREFIX = secret_patterns.ALLOWLIST_LINE_PREFIX

# Both spellings are honoured — see core/secret_patterns.PRAGMA_RE.
_PRAGMA_RE = secret_patterns.PRAGMA_RE

shannon_entropy = secret_patterns.shannon_entropy

# The allowlist is shared with the push gate so a file or line exempted for
# one is exempted for the other.
Allowlist = secret_patterns.Allowlist
load_allowlist = secret_patterns.load_allowlist


class GitError(RuntimeError):
    """git could not be run or exited non-zero. The scan must not pass."""


@dataclass(frozen=True)
class Pattern:
    """One reviewed detection rule. ``name`` shows up in the failure output."""

    name: str
    regex: re.Pattern[str] | None
    hint: str

    def search(self, line: str) -> tuple[str, str] | None:
        """Return ``(secret, prefix)`` for a hit, or None.

        ``secret`` is the credential text (never printed unredacted);
        ``prefix`` is the non-secret context inside the match, if any.
        """
        m = self.regex.search(line)
        if not m:
            return None
        if "v" in m.groupdict() and m.group("v") is not None:
            return m.group("v"), line[m.start() : m.start("v")]
        return m.group(0), ""


class _GenericAssignPattern(Pattern):
    """``key = <literal>`` — shared rule with placeholder/indirection filtering."""

    def search(self, line: str) -> tuple[str, str] | None:
        hit = secret_patterns.find_generic(line)
        return (hit.secret, hit.prefix) if hit else None


# Every rule comes from the table shared with the push-time gate so the two
# cannot drift apart. Literal patterns first (narrower labels), then the
# generic assignment rule.
PATTERNS: tuple[Pattern, ...] = tuple(
    Pattern(name=p.name, regex=p.regex, hint=p.hint) for p in secret_patterns.PATTERNS
) + (
    _GenericAssignPattern(
        name=secret_patterns.GENERIC_RULE,
        regex=None,
        hint=secret_patterns.GENERIC_HINT,
    ),
)

# Filenames that should never be committed, even if .gitignore is bypassed
# with `git add -f`. Checked against the repo-relative staged path.
BLOCKED_FILENAMES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$|sample$|template$|dist$)[^/]+$"),
    re.compile(r"(^|/)id_(?:rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"\.(?:pem|pfx|p12|keystore|jks|ppk|p8)$"),
    re.compile(r"(^|/)[^/]+\.key$"),
    re.compile(r"(^|/)\.aws/credentials$"),
    re.compile(r"(^|/)\.npmrc$"),
    re.compile(r"(^|/)\.pypirc$"),
    re.compile(r"(^|/)\.netrc$"),
    re.compile(r"(^|/)\.git-credentials$"),
    re.compile(r"(^|/)\.htpasswd$"),
    re.compile(r"(^|/)\.docker/config\.json$"),
    re.compile(r"(^|/)kubeconfig$"),
    re.compile(r"(^|/)(?:service[-_]?account[^/]*|credentials)\.json$"),
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


def _coerce_allowlist(obj) -> Allowlist:
    """Accept an ``Allowlist`` or a bare iterable of path regexes."""
    if isinstance(obj, Allowlist):
        return obj
    return Allowlist(paths=tuple(obj))


def _redact(secret: str) -> str:
    """Show only enough of *secret* to locate it, never the whole value.

    The failure message is printed to a terminal and, via the push gate, into a
    Claude transcript — both places a credential must not be re-leaked.
    """
    secret = secret.strip()
    if len(secret) <= 8:
        return secret[:2] + "***"
    return f"{secret[:4]}…{secret[-2:]} ({len(secret)} chars)"


def _git(args: Sequence[str], cwd: Path) -> str:
    """Run a git command and return stdout. Raises ``GitError`` on any failure."""
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
    except (OSError, ValueError) as exc:
        raise GitError(f"could not run git: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        raise GitError(f"git {' '.join(args[:2])} failed: {detail}")
    return proc.stdout


def repo_root(start: Path) -> Path | None:
    try:
        out = _git(["rev-parse", "--show-toplevel"], start).strip()
    except GitError:
        return None
    return Path(out) if out else None


def _unquote_path(target: str) -> str:
    """Undo git's C-style quoting of unusual paths (``"b/a \\"b\\".py"``)."""
    if len(target) >= 2 and target[0] == '"' and target[-1] == '"':
        return re.sub(r"\\(.)", r"\1", target[1:-1])
    return target


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
            target = _unquote_path(raw[4:].strip())
            # "+++ b/some/path" — or /dev/null for a deletion.  # portability-ok: diff syntax
            # git emits the literal string below; it is diff syntax, not a path
            # we open, so the portable-devnull rule doesn't apply.
            path = (
                "" if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            )  # portability-ok: diff syntax
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


def _skipped(path: str) -> bool:
    return any(rx.search(path) for rx in SKIP_CONTENT)


def scan_lines(
    lines: Iterable[tuple[str, int, str]],
    allowlist=(),
    entropy: bool = False,
) -> list[Finding]:
    """Apply every pattern to each ``(path, line_no, text)`` triple."""
    allow = _coerce_allowlist(allowlist)
    findings: list[Finding] = []
    for path, line_no, text in lines:
        if _skipped(path) or allow.allows_path(path) or allow.allows_line(text):
            continue
        for pattern in PATTERNS:
            hit = pattern.search(text)
            if hit:
                secret, prefix = hit
                findings.append(
                    Finding(path, line_no, pattern.name, pattern.hint, prefix + _redact(secret))
                )
                break  # one finding per line is enough to block
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


def blocked_files(paths: Iterable[str], allowlist=()) -> list[Finding]:
    """Findings for staged paths that should never be committed at all."""
    allow = _coerce_allowlist(allowlist)
    out: list[Finding] = []
    for path in paths:
        if not path or allow.allows_path(path):
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


_GIT_DIFF_OPTS = ("-c", "core.quotepath=false")


def staged_paths(root: Path) -> list[str]:
    out = _git([*_GIT_DIFF_OPTS, "diff", "--cached", "--name-only", "--diff-filter=ACMR"], root)
    return [_unquote_path(line.strip()) for line in out.splitlines() if line.strip()]


def scan_staged(root: Path, entropy: bool = False) -> list[Finding]:
    """Scan what a commit would add. Raises ``GitError`` if git cannot be run."""
    allowlist = load_allowlist(root)
    diff = _git(
        [
            *_GIT_DIFF_OPTS,
            "diff",
            "--cached",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
        ],
        root,
    )
    findings = blocked_files(staged_paths(root), allowlist)
    findings.extend(scan_lines(parse_staged_diff(diff), allowlist, entropy))
    return findings


def _git_visible_files(root: Path, target: Path) -> list[Path] | None:
    """Tracked + untracked-but-not-ignored files under *target*, via git.

    An ad-hoc ``--path`` scan inside a repo should see what a commit could
    see: runtime logs and caches that ``.gitignore`` already excludes are
    noise (and, being local-only, not a leak). Returns None outside a repo or
    if git fails, so the caller can fall back to walking the tree.
    """
    try:
        out = _git(
            [
                *_GIT_DIFF_OPTS,
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                str(target),
            ],
            root,
        )
    except GitError:
        return None
    return [root / _unquote_path(p) for p in out.split("\0") if p]


def scan_path(target: Path, root: Path | None = None, entropy: bool = False) -> list[Finding]:
    """Scan every readable text file under ``target`` (ad-hoc, not commit-time).

    Inside a git repo, ``.gitignore``d files are skipped — they cannot be
    committed, so they are not what this scanner guards against.
    """
    base = root or target
    allowlist = load_allowlist(base)
    triples: list[tuple[str, int, str]] = []
    paths: list[str] = []
    files = None
    if target.is_dir() and root is not None:
        files = _git_visible_files(root, target)
    if files is None:
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
        if _skipped(rel):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: nothing to scan
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
    print(f"  - add a regex for it to {ALLOWLIST_FILENAME} in the repo root", file=sys.stderr)
    print(
        f"    (a path regex exempts the file; '{ALLOWLIST_LINE_PREFIX}<regex>' exempts matching lines).",
        file=sys.stderr,
    )
    if hook_mode:
        print(
            "  - last resort: git commit --no-verify (skips ALL pre-commit hooks).", file=sys.stderr
        )
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
        try:
            findings = scan_staged(root, entropy=args.entropy)
        except GitError as exc:
            print(f"secret-scan: the scan did NOT run — {exc}", file=sys.stderr)
            print(
                "secret-scan: refusing to pass an unscanned commit; fix git and retry,",
                file=sys.stderr,
            )
            print("             or bypass once with: git commit --no-verify", file=sys.stderr)
            return 2
    else:
        target = args.path.expanduser()
        if not target.exists():
            print(f"secret-scan: no such path: {target}", file=sys.stderr)
            return 2
        findings = scan_path(
            target, repo_root(target if target.is_dir() else target.parent), args.entropy
        )

    if findings:
        report(findings, hook_mode=args.staged)
        return 1
    if not args.quiet:
        print("secret-scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
