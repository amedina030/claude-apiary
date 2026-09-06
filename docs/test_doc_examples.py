#!/usr/bin/env python3
"""Run every documented apiary command line.

Review §5a-D.2: *extract every ```bash block that invokes an apiary CLI and run
it in CI with --help*. A command that no longer parses — a renamed script, a
deleted subcommand, a launcher idiom that lost its target — fails here instead
of failing the person who copy-pasted it.

What it does, per fenced ```bash block in the documentation set:

1. Split each line into pipeline segments and find the apiary target: a
   repo-relative ``.py`` path, a ``-m <module>``, or the ``apiary`` console
   script — behind any number of launcher wrappers
   (``python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" scribe/notes.py …``,
   ``python "$L" …``, ``poetry run python …``).
2. Assert the target file exists. That is the "documented path that does not
   exist" half of the check, and it costs nothing.
3. Re-run it as ``<target> [<subcommand>] --help`` and require argparse to
   answer. The real arguments are dropped: a doc example is full of
   placeholders, and several of these commands spawn Claude, write state or
   touch a scheduler. ``--help`` exercises the part that can rot — the parser —
   without any of that. (``--dry-run`` was the alternative; several tools give
   it a different meaning — `compass/classify.py` prints the prompt — so it
   is not a safe universal substitute.)
4. Assert every ``--flag`` the example passes is a flag that parser really has.

Escape hatch: put ``<!-- no-run -->`` on the line before a fence to skip the
whole block (a shell snippet that is illustrative, not runnable).

The ``--help`` calls go through ``check_cli_claims``, so they hit the same
cached, thread-pooled introspection the generators use — the whole module runs
in about a second once anything else has warmed the cache.
"""

import re
import sys
import unittest
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent
for _p in (str(DOCS_DIR), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_cli_claims as cc  # noqa: E402

#: Documents whose bash blocks are executable claims.
DOC_GLOBS = ("docs/**/*.md", "*/commands/*.md", "*/CLAUDE.md")
DOC_FILES = ("README.md", "SETUP.md", "PORTABILITY.md", "RELEASING.md")

#: Never scanned: the review appendices are dated snapshots of a tree that has
#: since changed on purpose (they are deleted at close-out, T-2026-271).
SKIP_DIRS = {"review"}

SKIP_MARKER = "<!-- no-run -->"
FENCE_RE = re.compile(r"^\s*```(\w*)\s*$")

#: Wrapper tokens to step over before the real target.
LAUNCHER_HINTS = ("launch.py", "$L", "${L}")
PREFIX_WORDS = {"poetry", "run", "python", "python3", "py", "-3", "sudo", "time",
                "echo", "cat", "exec"}

#: Targets that exist but must not be executed or cannot be introspected.
UNRUNNABLE = set(cc.SKIP_HEADERS)

#: Documented commands that take no arguments at all and *act* when run, so
#: `--help` is not a safe probe. `scripts/install_repo_hooks.py` has no
#: argparse (`main()` at :111 reads no argv): running it with `--help`
#: installs the git hooks rather than printing usage. Out of scope for the
#: docs phase — filed as a finding, not fixed here — but it must not be
#: executed by the suite in the meantime.
SIDE_EFFECTING = {
    "scripts/install_repo_hooks.py",
}


def iter_docs():
    """Every documentation file whose bash blocks are checked."""
    seen: set[Path] = set()
    for name in DOC_FILES:
        p = REPO_ROOT / name
        if p.is_file():
            seen.add(p)
    for pattern in DOC_GLOBS:
        for p in REPO_ROOT.glob(pattern):
            if not p.is_file() or p.suffix != ".md":
                continue
            if SKIP_DIRS & set(p.relative_to(REPO_ROOT).parts):
                continue
            seen.add(p)
    return sorted(seen)


def bash_blocks(text: str):
    """Yield ``(first_line_number, [lines])`` for each runnable bash fence."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = FENCE_RE.match(lines[i])
        if not m or m.group(1) not in ("bash", "sh", "shell"):
            i += 1
            continue
        skip = any(SKIP_MARKER in lines[j] for j in range(max(0, i - 2), i))
        start = i + 1
        j = start
        while j < len(lines) and not FENCE_RE.match(lines[j]):
            j += 1
        if not skip:
            yield start + 1, lines[start:j]
        i = j + 1


def _segments(line: str):
    """Split a shell line into pipeline segments."""
    return [s for s in re.split(r"\|\||&&|[|;]", line) if s.strip()]


def _tokens(segment: str) -> list[str]:
    """Whitespace tokens with quotes stripped and `$(...)` kept whole."""
    out, buf, quote, depth = [], [], "", 0
    for ch in segment.strip():
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch.isspace() and depth == 0:
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def find_target(segment: str) -> tuple[str, list[str]] | None:
    """Return ``(repo-relative target, args)`` for an apiary invocation.

    ``None`` when the segment does not invoke one — a `git` call, a `pip`
    install, `python -m pytest`, a shell builtin.
    """
    tokens = _tokens(segment)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if any(hint in tok for hint in LAUNCHER_HINTS):   # step over the launcher
            i += 1
            continue
        if tok in PREFIX_WORDS or tok.startswith("$") or tok.endswith("="):
            i += 1
            continue
        if tok == "-m" and i + 1 < len(tokens):
            rel = tokens[i + 1].replace(".", "/") + ".py"
            return (rel, tokens[i + 2:]) if (REPO_ROOT / rel).is_file() else None
        if tok == "apiary":
            return "core/cli.py", tokens[i + 1:]
        if tok.endswith(".py"):
            rel = tok.lstrip("./")
            return (rel, tokens[i + 1:]) if (REPO_ROOT / rel).is_file() else None
        return None
    return None


def documented_calls():
    """Every ``(doc, line, target, args)`` an apiary CLI is invoked with."""
    calls = []
    for doc in iter_docs():
        text = doc.read_text(encoding="utf-8")
        rel_doc = doc.relative_to(REPO_ROOT).as_posix()
        for first_line, block in bash_blocks(text):
            for offset, line in enumerate(block):
                if line.strip().startswith("#") or not line.strip():
                    continue
                for segment in _segments(line):
                    found = find_target(segment)
                    if found is None:
                        continue
                    rel, args = found
                    calls.append((rel_doc, first_line + offset, rel, args))
    return calls


def _named_path_claims(rel: str) -> bool:
    """True when a documented `.py` target must not be executed."""
    name = Path(rel).name
    return (rel in UNRUNNABLE or rel in SIDE_EFFECTING
            or name.startswith("test_") or rel.startswith("gui/"))


class DocExampleTests(unittest.TestCase):
    """Every documented apiary command still parses."""

    @classmethod
    def setUpClass(cls):
        cls.calls = documented_calls()
        targets = sorted({rel for _d, _l, rel, _a in cls.calls
                          if not _named_path_claims(rel)})
        cls.ifaces = cc.inspect_tools(targets)

    def test_the_scan_found_something(self):
        # A refactor that breaks the extractor must not silently pass the suite.
        self.assertGreater(len(self.calls), 30,
                           "the bash-block extractor found almost nothing — "
                           "it is probably broken, not the docs")

    def test_every_documented_target_exists(self):
        for doc, line, rel, _args in self.calls:
            with self.subTest(doc=doc, line=line, target=rel):
                self.assertTrue((REPO_ROOT / rel).is_file(),
                                f"{doc}:{line} documents {rel}, which does not exist")

    def test_every_documented_command_still_parses(self):
        for doc, line, rel, args in self.calls:
            if _named_path_claims(rel):
                continue
            iface = self.ifaces.get(rel)
            with self.subTest(doc=doc, line=line, target=rel):
                self.assertNotIsInstance(
                    iface, cc.CannotIntrospect,
                    f"{doc}:{line} — `{rel} --help` failed: {iface}")

    def test_every_documented_subcommand_exists(self):
        for doc, line, rel, args in self.calls:
            if _named_path_claims(rel):
                continue
            iface = self.ifaces.get(rel)
            if not isinstance(iface, cc.ToolInterface) or not iface.subcommands:
                continue
            sub = next((a for a in args
                        if re.fullmatch(r"[a-z][a-z0-9-]*", a)), None)
            if sub is None:
                continue
            with self.subTest(doc=doc, line=line, target=rel, subcommand=sub):
                self.assertIn(sub, iface.subcommands,
                              f"{doc}:{line} documents `{rel} {sub}`, "
                              f"which argparse does not have")

    def test_every_documented_flag_exists(self):
        # Known limit: ToolInterface.flags is the union over every subparser, so
        # a real flag documented on the WRONG subcommand still passes here; the
        # unknown-flag case is what this catches (per-subparser check: T-2026-291).
        for doc, line, rel, args in self.calls:
            if _named_path_claims(rel):
                continue
            iface = self.ifaces.get(rel)
            if not isinstance(iface, cc.ToolInterface):
                continue
            for arg in args:
                flag = arg.split("=", 1)[0]
                if not re.fullmatch(r"--[a-z][\w-]*", flag):
                    continue
                with self.subTest(doc=doc, line=line, target=rel, flag=flag):
                    self.assertIn(flag, iface.flags,
                                  f"{doc}:{line} documents `{rel} {flag}`, "
                                  f"which argparse does not have")


if __name__ == "__main__":
    unittest.main()
