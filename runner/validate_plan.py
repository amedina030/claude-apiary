#!/usr/bin/env python3
"""
Validate a plan JSON file for the autonomous runner.

Checks:
- Required top-level fields (uuid, executor_model, spec, steps)
- Required per-step fields (step_number, type, description, action, files, depends_on, code_spec)
- Valid step types and actions
- File existence for modify/delete actions (create actions skip this check)
- No circular dependencies between steps
- Every acceptance criterion from the embedded spec is covered by at least one step

Exit 0 + prints "Valid" on success.
Exit 1 + prints error details on failure.

Usage:
    validate_plan.py <path_to_plan.json>
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

VALID_TYPES = {"create", "modify", "delete", "test", "verify"}
VALID_ACTIONS = {"create", "modify", "delete", "test", "verify"}
VALID_MODELS = {"opus", "sonnet", "haiku"}
REQUIRED_STEP_FIELDS = ["step_number", "type", "description", "action", "files", "depends_on", "code_spec"]

# Words that indicate a test code_spec is prose, not a shell command. The
# executor passes test code_spec directly to subprocess.run(shell=True), so
# 'Run python -m pytest ...' tries to execute literal 'Run' as a binary.
_PROSE_STARTERS = {
    "run", "execute", "use", "call", "then", "now", "this", "make",
    "please", "here", "the", "we", "you",
}

# Banned tokens — substrings that, if found in any step's code_spec or
# description, indicate the planner ignored project conventions documented
# in CLAUDE.md and docs/standards/code-style.md. Hard rule violations only:
# things the codebase forbids outright. Extend this map as new violations
# are caught in runner runs. The reason string is shown to the planner
# on retry so it can correct course.
_BANNED_TOKENS = {
    "pytest": "use unittest (stdlib) — see docs/standards/code-style.md",
    "shell=true": "shell=True is banned — use list-form subprocess args",
    "import requests": "external dependencies are banned — stdlib only",
    "from requests": "external dependencies are banned — stdlib only",
}

# Word-boundary-aware patterns for each banned token (#239). Plain
# substring match would false-positive on 'from requests_mock' matching
# the 'from requests' ban, or 'requests_toolbelt' matching 'requests'.
# The (?<!\w)...(?!\w) guards ensure we only match when the token is
# bounded by non-word characters (or the start/end of the haystack),
# which correctly rejects identifier-suffix false positives while still
# catching 'pytest-asyncio' (hyphen IS a word boundary).
_BANNED_TOKEN_PATTERNS = {
    token: re.compile(rf"(?<!\w){re.escape(token)}(?!\w)")
    for token in _BANNED_TOKENS
}

# Phrases that, when found in a test-action step's description, indicate the
# planner is using a test step as a gating audit run rather than a pass/fail
# verification. The executor treats every test step as a hard gate (non-zero
# exit aborts the plan), so a "this run is expected to report violations"
# step always aborts. Pattern caught in T5b plan step 3 (#211). The fix is
# either to make the audit step a non-test type, or to make it a test step
# whose code_spec already includes the violation-handling logic.
_TEST_FAILURE_LANGUAGE = (
    "expected to fail",
    "expected to report violations",
    "expected to enumerate violations",
    "expected to error",
    "should fail",
    "this run is expected to",
)


# --- Path allowlist (#212, revised) ---
#
# Runner plans must operate exclusively on files inside the repo working
# tree. Out-of-repo paths are rejected outright — including legitimate
# state files under ~/.claude/projects/<project-key>/ — because the
# executor's git-based verifier can't see anything outside the worktree
# and we don't want a parallel non-git verification path. Hand-fix any
# ticket whose work would touch out-of-repo state.
#
# Every step.files entry is resolved via _resolve_repo_path — relative
# paths are joined against REPO_ROOT explicitly (NOT cwd), absolute paths
# against themselves — and the result must fall under REPO_ROOT. Resolving
# against REPO_ROOT instead of cwd means validate_plan can be invoked from
# anywhere without path rejection false positives (#232).

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_repo_path(path: Path) -> Path:
    """Resolve `path` relative to REPO_ROOT if not already absolute.

    Never uses cwd as the resolution base — the runner orchestrator happens
    to cd to repo root today, but validate_plan must not rely on that
    implicit invariant (#232).
    """
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()


def _rel_under_repo(resolved: Path):
    """Return the repo-relative POSIX path string for `resolved`, or None
    if it's not under REPO_ROOT.

    Comparison is case-insensitive on Windows (via os.path.normcase) so
    that a planner-emitted path like 'D:\\professional\\claude-apiary\\..'
    resolves correctly against an actual repo root of
    'D:\\Professional\\claude-apiary\\..' (#234). On POSIX normcase is a
    no-op, preserving the existing case-sensitive behavior.
    """
    repo = str(_REPO_ROOT.resolve())
    r = str(resolved)
    repo_n = os.path.normcase(repo)
    r_n = os.path.normcase(r)
    if r_n == repo_n:
        return ""
    sep = os.sep
    prefix = repo_n if repo_n.endswith(sep) else repo_n + sep
    if not r_n.startswith(prefix):
        return None
    # normcase only changes case (and slash direction on Windows) — never
    # length — so slicing the original string by len(prefix) safely strips
    # the root while preserving the on-disk case of the tail.
    rel = r[len(prefix):]
    return rel.replace("\\", "/")


def _path_under_repo(path: Path) -> bool:
    try:
        resolved = _resolve_repo_path(path)
    except (OSError, RuntimeError):
        return False
    return _rel_under_repo(resolved) is not None


def _detect_cycles(steps: list[dict]) -> list[str]:
    """Detect circular dependencies in step graph. Returns error strings."""
    # Build adjacency: step_number -> list of step_numbers it depends on
    step_nums = {s["step_number"] for s in steps if isinstance(s.get("step_number"), int)}
    graph = {}
    for s in steps:
        num = s.get("step_number")
        deps = s.get("depends_on", [])
        if isinstance(num, int) and isinstance(deps, list):
            graph[num] = [d for d in deps if isinstance(d, int)]

    # DFS cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    errors = []

    def dfs(node, path):
        color[node] = GRAY
        path.append(node)
        for dep in graph.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                # Found cycle
                cycle_start = path.index(dep)
                cycle = path[cycle_start:] + [dep]
                errors.append(f"Circular dependency detected: {' -> '.join(str(n) for n in cycle)}")
                return
            if color[dep] == WHITE:
                dfs(dep, path)
        path.pop()
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            dfs(node, [])

    return errors


def _check_test_code_spec_format(steps: list[dict]) -> list[str]:
    """Reject test-action steps whose code_spec is prose, not a shell command.

    The executor's run_test_command does subprocess.run(code_spec, shell=True),
    so the planner MUST emit a single shell command for test steps. Two cheap
    heuristics catch the failure mode that bit T4 step 6 ('Run python -m
    pytest ...' tried to execute literal 'Run' as a Windows binary):

    1. The stripped code_spec must not contain newlines (real commands fit
       on one line; prose almost never does).
    2. The first whitespace-separated token must not be a known prose starter
       like 'Run', 'Execute', 'Use', etc.
    """
    errors = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if step.get("action") != "test":
            continue
        code_spec = step.get("code_spec", "")
        if not isinstance(code_spec, str):
            continue
        stripped = code_spec.strip()
        if not stripped:
            # 'empty' is already caught by the required-field check
            continue
        if "\n" in stripped:
            errors.append(
                f"step[{i}] (action='test'): code_spec must be a single shell "
                f"command on one line — no newlines, no prose. The executor "
                f"passes it directly to subprocess.run(shell=True)."
            )
            continue
        first_word = stripped.split(None, 1)[0].rstrip(":,.").lower()
        if first_word in _PROSE_STARTERS:
            errors.append(
                f"step[{i}] (action='test'): code_spec starts with prose word "
                f"'{first_word}' — must begin with the actual shell command "
                f"itself (e.g. 'python -m unittest ...')."
            )
    return errors


def _check_test_failure_language(steps: list[dict]) -> list[str]:
    """Reject test-action steps whose description signals an expected failure.

    The executor treats every test step as a hard pass/fail gate (non-zero
    exit aborts the plan). A test step described as "expected to report
    violations" or "this run is expected to fail" therefore always aborts
    the plan, defeating its purpose. The planner should either drop the
    gating run entirely, mark it as a non-test step type, or wrap it in
    code_spec logic that turns the violations into a real assertion.
    Caught in T5b plan step 3 (#211).
    """
    errors = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if step.get("action") != "test":
            continue
        description = step.get("description", "")
        if not isinstance(description, str):
            continue
        lower = description.lower()
        for phrase in _TEST_FAILURE_LANGUAGE:
            if phrase in lower:
                errors.append(
                    f"step[{i}] (action='test'): description signals expected "
                    f"failure ('{phrase}'). Test steps are hard gates — a non-"
                    f"zero exit aborts the plan. Either change the step type "
                    f"away from 'test' or wrap the audit logic in code_spec "
                    f"so it returns 0 on the expected condition."
                )
                break
    return errors


def _check_path_allowlist(steps: list[dict]) -> list[str]:
    """Reject plans whose step.files contain paths outside the repo (#212).

    Every file entry is resolved — relative paths against the runner's
    cwd (always repo root), absolute paths against themselves — and the
    result must fall under REPO_ROOT. Catches both T5b-style absolute
    Windows paths and `../../etc/passwd` style escapes.

    Out-of-repo paths are rejected even when they target legitimate
    persistent-state locations (e.g. ~/.claude/projects/<key>/) — those
    must be hand-fixed, because the executor's git-based verifier
    cannot see anything outside the worktree.
    """
    errors = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        files = step.get("files", [])
        if not isinstance(files, list):
            continue
        for f in files:
            if not isinstance(f, str) or not f:
                continue
            if not _path_under_repo(Path(f)):
                errors.append(
                    f"step[{i}]: path '{f}' is outside the repo working "
                    f"tree (runner only operates on in-repo files; "
                    f"hand-fix if you need to touch out-of-repo state)"
                )
    return errors


def _check_gitignored_paths(steps: list[dict]) -> list[str]:
    """Reject plans whose step.files contain gitignored paths (#233).

    A plan targeting e.g. `runner/specs/<uuid>.json` (a gitignored path)
    passes the path allowlist, then at commit time `git add` silently
    no-ops, `git diff --cached --quiet` returns 0, and commit_files
    raises a generic "subprocess made no changes" error. Catching it
    here surfaces the real cause to the planner on retry.

    Uses `git check-ignore --stdin` to batch-check all file paths at
    once. Paths are passed as repo-relative strings; out-of-repo paths
    are skipped (they're already rejected by the allowlist). If git is
    unavailable or the call fails for any reason, the check is skipped
    silently — we don't want a missing git binary to break validation.
    """
    # Collect (step_index, raw_path, rel_path) for every in-repo file entry.
    entries: list[tuple[int, str, str]] = []
    repo_root = _REPO_ROOT.resolve()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        files = step.get("files", [])
        if not isinstance(files, list):
            continue
        for f in files:
            if not isinstance(f, str) or not f:
                continue
            try:
                resolved = _resolve_repo_path(Path(f))
            except (OSError, RuntimeError):
                continue
            rel = _rel_under_repo(resolved)
            if rel is None or rel == "":
                # Outside repo (handled by allowlist) or is the repo
                # root itself (nothing to check-ignore).
                continue
            entries.append((i, f, rel))

    if not entries:
        return []

    # Use -z (NUL-separated) mode for both stdin and stdout to avoid
    # Windows CRLF translation on the stdin pipe, which otherwise
    # corrupts paths with a trailing \r and breaks parsing.
    stdin_blob = ("\x00".join(rel for _, _, rel in entries) + "\x00").encode("utf-8")
    try:
        result = subprocess.run(
            # No --no-index: we WANT git to respect the index, so that
            # a tracked path never shows up as ignored even if it also
            # matches a .gitignore pattern. Only untracked-and-ignored
            # paths should be rejected.
            ["git", "check-ignore", "-z", "--stdin"],
            input=stdin_blob,
            cwd=str(repo_root),
            capture_output=True,
        )
    except (FileNotFoundError, OSError):
        # Git not available — skip the check silently.
        return []

    # `git check-ignore` exits 0 if any path is ignored, 1 if none, 128 on
    # error. Anything else = skip.
    if result.returncode not in (0, 1):
        return []

    stdout_text = result.stdout.decode("utf-8", errors="replace")
    ignored = {p for p in stdout_text.split("\x00") if p}
    if not ignored:
        return []

    errors: list[str] = []
    for i, raw, rel in entries:
        if rel in ignored:
            errors.append(
                f"step[{i}]: path '{raw}' is gitignored — git add would "
                f"silently no-op and commit_files would raise a generic "
                f"'no changes' error. Either un-ignore the path or "
                f"target a tracked location."
            )
    return errors


def _check_banned_tokens(steps: list[dict]) -> list[str]:
    """Reject plans whose code_spec or description references banned tokens.

    The planner has access to CLAUDE.md and docs/standards/code-style.md and
    is explicitly told the hard rules in the auto_plan prompt. If it still
    proposes pytest, shell=True, or external imports, the validator catches
    it before any code gets written. Errors flow into the existing 3-attempt
    retry loop in auto_plan, so the planner gets a chance to self-correct.
    """
    errors = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        haystack = " ".join([
            str(step.get("code_spec", "")),
            str(step.get("description", "")),
        ]).lower()
        for token, reason in _BANNED_TOKENS.items():
            if _BANNED_TOKEN_PATTERNS[token].search(haystack):
                errors.append(
                    f"step[{i}]: banned token '{token}' found in plan — {reason}"
                )
    return errors


_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does",
    "for", "from", "has", "have", "in", "is", "it", "its", "of", "on",
    "or", "the", "this", "that", "to", "was", "with", "will",
})

# Strip punctuation/symbols for tokenization — re.findall(r"[a-z0-9_]+")
# catches alphanumeric tokens and discards everything else.
_WORD_RE = re.compile(r"[a-z0-9_]+")


def _criterion_bigrams(criterion: str) -> list:
    """Extract content bigrams from an acceptance criterion.

    Lowercases, tokenizes on word chars, then emits every adjacent word
    pair joined by a single space — EXCEPT pairs where both tokens are
    stopwords (a stopword-only bigram like 'to the' carries no signal
    and would match almost any step text).

    Returns an empty list if the criterion is too short to produce any
    usable bigram, in which case _check_criteria_coverage skips the
    criterion entirely rather than reporting a false-positive error.
    """
    tokens = _WORD_RE.findall(criterion.lower())
    bigrams = []
    for a, b in zip(tokens, tokens[1:]):
        if a in _STOPWORDS and b in _STOPWORDS:
            continue
        bigrams.append(f"{a} {b}")
    return bigrams


def _transitive_deps(steps: list) -> dict:
    """Return {step_number: set(of all transitive ancestor step numbers)}.

    Walks the depends_on graph from every node so callers can cheaply
    ask 'is step A an ancestor of step B?' — used by the file-overlap
    check (#241) to verify that two steps touching the same file are
    ordered by an explicit dependency chain.
    """
    direct = {}
    for s in steps:
        if not isinstance(s, dict):
            continue
        num = s.get("step_number")
        if not isinstance(num, int):
            continue
        deps = s.get("depends_on", [])
        direct[num] = [d for d in deps if isinstance(d, int)] if isinstance(deps, list) else []

    ancestors = {}
    for start in direct:
        seen = set()
        stack = list(direct.get(start, []))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(direct.get(cur, []))
        ancestors[start] = seen
    return ancestors


def _check_file_overlap(steps: list) -> list:
    """Reject plans where multiple steps write to the same file without
    a depends_on chain between them (#241).

    Without this, topological sort may pick either order, and the two
    steps race: step 7's write may land before step 3's, the runner
    commits both, and the final state is nondeterministic across runs.
    The fix is an explicit dependency (step 7 depends_on step 3) or
    merging the two into one step. The error message lists the smaller
    step number as the suggested dependency target because that's the
    intuitive "this edit came first" direction.
    """
    ancestors = _transitive_deps(steps)

    file_to_steps: dict = {}
    for s in steps:
        if not isinstance(s, dict):
            continue
        num = s.get("step_number")
        if not isinstance(num, int):
            continue
        files = s.get("files", [])
        if not isinstance(files, list):
            continue
        for f in files:
            if isinstance(f, str) and f:
                key = f.replace("\\", "/").strip()
                file_to_steps.setdefault(key, []).append(num)

    errors = []
    reported_pairs = set()
    for f, nums in file_to_steps.items():
        if len(nums) < 2:
            continue
        unique = sorted(set(nums))
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                a, b = unique[i], unique[j]
                if a in ancestors.get(b, set()) or b in ancestors.get(a, set()):
                    continue
                pair_key = (a, b, f)
                if pair_key in reported_pairs:
                    continue
                reported_pairs.add(pair_key)
                errors.append(
                    f"steps {a} and {b} both target '{f}' without a "
                    f"depends_on chain — execution order is undefined "
                    f"and the two steps may race. Either merge them or "
                    f"add depends_on: [{a}] to step {b}."
                )
    return errors


_REMOVAL_KEYWORDS = re.compile(
    r'\b(remove|delete|drop|rip out|eliminate|strip)\b.*\b(function|method|class|constant|import|variable|attr)\b'
    r'|\b(function|method|class|constant|import|variable|attr)\b.*\b(remove|delete|drop|rip out|eliminate|strip)\b',
    re.IGNORECASE,
)

# Match symbols that look like Python identifiers: UPPER_CASE, snake_case with
# underscores, or CamelCase. Plain lowercase words like 'learning' are too
# common and produce false positives when grepped across the codebase.
_SYMBOL_IN_QUOTES = re.compile(r"['\"`]([A-Za-z_][A-Za-z0-9_]*)['\"`]")

# Match bare (unquoted) Python-style identifiers. Planner output often
# mentions symbols like LEARNING_FOLDER or add_learning without quoting
# them. We extract all word-shaped tokens and let _is_python_symbol()
# filter down to real identifiers.
_BARE_IDENTIFIER = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


def _is_python_symbol(name: str) -> bool:
    """Return True if name looks like a Python symbol, not a plain English word."""
    if len(name) < 6:
        return False
    # UPPER_CASE constants (e.g. LEARNING_FOLDER, TYPE_FOLDERS)
    if name.isupper() and '_' in name:
        return True
    # snake_case with underscore (e.g. add_learning, list_learnings)
    if name.islower() and '_' in name:
        return True
    # CamelCase classes (e.g. ScribeStore)
    if name[0].isupper() and any(c.islower() for c in name) and any(c.isupper() for c in name[1:]):
        return True
    return False


def _check_removal_coverage(steps: list[dict]) -> list[str]:
    """Warn when a step removes a Python symbol but not all files importing it are in the plan.

    Only checks symbols that look like Python identifiers (UPPER_CASE constants,
    snake_case functions, CamelCase classes). Uses git grep with word-boundary
    patterns to find actual code references, not prose mentions.
    """
    errors = []

    # Collect all files covered by any step in the plan
    plan_files: set[str] = set()
    for s in steps:
        if not isinstance(s, dict):
            continue
        for f in s.get("files", []):
            if isinstance(f, str) and f:
                plan_files.add(f.replace("\\", "/").strip())

    # Find steps that remove symbols
    seen_symbols: set[str] = set()  # dedup across steps
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        action = step.get("action", "")
        desc = step.get("description", "")
        code_spec = step.get("code_spec", "")
        combined = f"{desc} {code_spec}"

        if action not in ("modify", "delete"):
            continue
        if not _REMOVAL_KEYWORDS.search(combined):
            continue

        # Extract symbol names from quoted and bare references in the
        # description/code_spec. Quoted symbols are preferred but planners
        # often mention symbols like LEARNING_FOLDER unquoted.
        symbols = set(_SYMBOL_IN_QUOTES.findall(combined))
        symbols.update(_BARE_IDENTIFIER.findall(combined))
        if not symbols:
            continue

        for symbol in symbols:
            if not _is_python_symbol(symbol):
                continue
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)

            # Use word-boundary grep to match actual Python references
            try:
                result = subprocess.run(
                    ["git", "grep", "-l", "-w", "--", symbol],
                    capture_output=True, text=True, timeout=10,
                    cwd=str(_REPO_ROOT),
                )
                if result.returncode != 0:
                    continue
                referencing_files = {
                    f.strip().replace("\\", "/")
                    for f in result.stdout.splitlines()
                    if f.strip()
                }
            except (OSError, subprocess.TimeoutExpired):
                continue

            # Filter to Python files only (where import/usage errors matter).
            # Exclude this validator's own file — it references symbols as
            # part of grep patterns and test fixtures, not real usage.
            _SELF = "runner/validate_plan.py"
            py_files = {
                f for f in referencing_files
                if f.endswith(".py") and f != _SELF
            }

            uncovered = py_files - plan_files
            if uncovered:
                errors.append(
                    f"step[{i}] removes symbol '{symbol}' but {len(uncovered)} "
                    f"file(s) referencing it are not in the plan: "
                    f"{', '.join(sorted(uncovered)[:5])}"
                    + (f" (and {len(uncovered) - 5} more)" if len(uncovered) > 5 else "")
                )

    return errors


def _check_criteria_coverage(spec: dict, steps: list[dict]) -> list[str]:
    """Check that every acceptance criterion is referenced by at least one step.

    #240: previously used a per-word 'any keyword >3 chars overlaps'
    heuristic, which false-positived constantly — a criterion of 'user
    can log in' was 'covered' by any step mentioning 'user'. We now
    require at least one bigram from the criterion to appear verbatim
    in the combined step text, which demands roughly-in-context phrase
    overlap rather than lone-word overlap.

    Criteria that produce no meaningful bigrams (too short, all
    stopwords) are skipped rather than always-error'd, because the
    validator can't check them reliably and a hard error would force
    the planner to mutilate short criteria just to satisfy the check.
    """
    criteria = spec.get("acceptance_criteria", [])
    if not isinstance(criteria, list) or not criteria:
        return []

    errors = []
    step_text = " ".join(
        f"{s.get('description', '')} {s.get('code_spec', '')}"
        for s in steps if isinstance(s, dict)
    ).lower()
    # Normalize step text tokens the same way as criterion tokens so the
    # bigram match is consistent across punctuation noise in code_spec.
    step_tokens = _WORD_RE.findall(step_text)
    step_normalized = " ".join(step_tokens)

    for i, criterion in enumerate(criteria):
        if not isinstance(criterion, str):
            continue
        bigrams = _criterion_bigrams(criterion)
        if not bigrams:
            continue
        if not any(bg in step_normalized for bg in bigrams):
            errors.append(
                f"Acceptance criterion [{i}] not covered by any step: "
                f"'{criterion[:80]}...'" if len(criterion) > 80 else
                f"Acceptance criterion [{i}] not covered by any step: '{criterion}'"
            )

    return errors


def validate(data: dict) -> list[str]:
    """Validate plan data and return list of error strings (empty = valid)."""
    errors = []

    if not isinstance(data, dict):
        return ["Expected a JSON object"]

    # Required top-level fields
    for field in ["uuid", "executor_model", "spec", "steps"]:
        if field not in data:
            errors.append(f"Missing required field '{field}'")

    if errors:
        return errors

    spec = data.get("spec", {})
    steps = data.get("steps", [])

    if not isinstance(steps, list):
        errors.append("'steps' must be an array")
        return errors

    if len(steps) == 0:
        errors.append("'steps' is empty — plan must have at least one step")
        return errors

    # Per-step validation
    seen_numbers = set()
    created_files = set()  # files that create steps will produce
    for i, step in enumerate(steps):
        label = f"step[{i}]"

        if not isinstance(step, dict):
            errors.append(f"{label}: not a JSON object")
            continue

        # Required fields
        for field in REQUIRED_STEP_FIELDS:
            val = step.get(field)
            if val is None:
                errors.append(f"{label}: missing required field '{field}'")
            elif isinstance(val, str) and not val.strip():
                errors.append(f"{label}: field '{field}' is empty")

        # Step number uniqueness
        num = step.get("step_number")
        if isinstance(num, int):
            if num in seen_numbers:
                errors.append(f"{label}: duplicate step_number {num}")
            seen_numbers.add(num)

        # Valid type and action
        step_type = step.get("type")
        if isinstance(step_type, str) and step_type not in VALID_TYPES:
            errors.append(f"{label}: invalid type '{step_type}' (expected: {', '.join(sorted(VALID_TYPES))})")

        action = step.get("action")
        if isinstance(action, str) and action not in VALID_ACTIONS:
            errors.append(f"{label}: invalid action '{action}' (expected: {', '.join(sorted(VALID_ACTIONS))})")

        # Optional per-step model field
        if "model" in step:
            model_val = step.get("model")
            if not isinstance(model_val, str):
                errors.append(f"{label}: field 'model' must be a string")
            elif model_val not in VALID_MODELS:
                errors.append(
                    f"{label}: invalid model '{model_val}' "
                    f"(expected: {', '.join(sorted(VALID_MODELS))})"
                )

        # Track files that create steps will produce, so modify/delete
        # steps can reference them without a "file not found" error.
        if action == "create":
            step_files = step.get("files", [])
            if isinstance(step_files, list):
                for f in step_files:
                    if isinstance(f, str):
                        created_files.add(f)

        # File existence for modify/delete actions. Resolve relative paths
        # against REPO_ROOT, not cwd (#232) — consistent with the path
        # allowlist check above. Skip files that a prior create step produces.
        if action in ("modify", "delete"):
            files = step.get("files", [])
            if isinstance(files, list):
                for f in files:
                    if isinstance(f, str) and f not in created_files and not _resolve_repo_path(Path(f)).exists():
                        errors.append(f"{label}: file not found: {f}")

        # depends_on references valid step numbers
        deps = step.get("depends_on", [])
        if isinstance(deps, list):
            for dep in deps:
                if isinstance(dep, int) and dep not in seen_numbers and dep >= num:
                    # Forward reference — may still be valid if the step exists later
                    pass  # Checked via cycle detection instead

    # Circular dependency check
    errors.extend(_detect_cycles(steps))

    # Test-action code_spec format check (must be a shell command, not prose)
    errors.extend(_check_test_code_spec_format(steps))

    # Test-action description must not signal expected failure (#211)
    errors.extend(_check_test_failure_language(steps))

    # Banned-token check (project convention violations: pytest, shell=True, etc.)
    errors.extend(_check_banned_tokens(steps))

    # Path allowlist (#212): reject absolute paths outside repo + state dir.
    errors.extend(_check_path_allowlist(steps))

    # Gitignored-path check (#233): surface the "git add no-ops, commit
    # raises generic 'no changes'" failure at validation time.
    errors.extend(_check_gitignored_paths(steps))

    # File-overlap check (#241): reject plans where multiple steps
    # write the same file without a depends_on chain.
    errors.extend(_check_file_overlap(steps))

    # Symbol removal coverage: reject plans that remove a symbol without
    # covering all files that reference it.
    errors.extend(_check_removal_coverage(steps))

    # Acceptance criteria coverage
    if isinstance(spec, dict):
        errors.extend(_check_criteria_coverage(spec, steps))

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate runner plan file")
    parser.add_argument("file", help="Path to plan JSON file")
    args = parser.parse_args()

    try:
        with open(args.file, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Strip top-level metadata before validation
    plan_data = {k: v for k, v in data.items() if k != "valid"}

    errors = validate(plan_data)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        sys.exit(1)

    print("Valid")


if __name__ == "__main__":
    main()
