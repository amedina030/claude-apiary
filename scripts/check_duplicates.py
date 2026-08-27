#!/usr/bin/env python3
"""AST near-duplicate detector for Python functions. Report-only.

The third layer of the duplication-prevention plan (review §5a-C): one
`core/utils/` with guessable names, a duplicate-helper nudge in the hook
dispatcher, and this — a check that finds the copies that already landed. The
doc-only rule ("Reuse core/", `docs/standards/code-style.md`) demonstrably
failed: the review counted the same `git rev-parse` block in eight files and
the same JSON reader in five.

Mechanism, stdlib only (like `secret_scan.py` — this has to be runnable from
a git hook, where the Poetry virtualenv is not importable):

1. Parse every `.py` file with `ast` and pull out each function/method.
2. Normalise its body: drop the docstring, rename arguments and locals to
   positional placeholders (`a0`, `v1`, …) so two copies that renamed a
   variable still match, and drop the function's own name.
3. Hash each normalised statement, and hash the whole body.

Identical body-hashes are exact duplicates whatever they are called. Bodies
that merely *share* statement hashes are scored by multiset Jaccard overlap
and reported above a threshold. Anything shorter than ``--min-statements``
is ignored: three-line helpers are supposed to look alike.

What it does NOT do: judge. A reported pair may be a parity test, a
deliberate mirror, or a real copy-paste. It exits 0 either way unless you ask
for ``--fail-on-identical``.

Usage::

    python scripts/check_duplicates.py                    # whole repo
    python scripts/check_duplicates.py --path core        # one subtree
    python scripts/check_duplicates.py --threshold 0.95   # only near-clones
    python scripts/check_duplicates.py --fail-on-identical

Exit codes: ``0`` report produced; ``1`` identical bodies found *and*
``--fail-on-identical`` was passed; ``2`` bad arguments or unreadable path.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that are never ours to deduplicate: virtualenvs, build output,
# per-target state, vendored JS, and the agent worktrees under .claude/.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".repos",
    ".scrap",
    ".claude",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".apiary",
    ".apiary.pre-migration",
    "site-packages",
}

DEFAULT_MIN_STATEMENTS = 8
DEFAULT_THRESHOLD = 0.85
DEFAULT_TOP = 25


@dataclass
class Function:
    """One normalised function body."""

    path: Path
    qualname: str
    lineno: int
    statements: int
    body_hash: str
    statement_hashes: list[str] = field(default_factory=list)

    @property
    def where(self) -> str:
        try:
            rel = self.path.relative_to(REPO_ROOT)
        except ValueError:
            rel = self.path
        return f"{rel.as_posix()}:{self.lineno}"

    def __str__(self) -> str:
        return f"{self.where} {self.qualname}() [{self.statements} stmts]"


class _Normalizer(ast.NodeTransformer):
    """Rewrite argument and local names to positional placeholders.

    Names that are *not* bound inside the function — imports, module-level
    constants, other functions — keep their identifiers, because two bodies
    calling different helpers are not duplicates however similar their shape.
    """

    def __init__(self, bound: dict[str, str]) -> None:
        self.bound = bound

    def visit_Name(self, node: ast.Name) -> ast.AST:
        replacement = self.bound.get(node.id)
        if replacement is None:
            return node
        return ast.copy_location(ast.Name(id=replacement, ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        replacement = self.bound.get(node.arg)
        new = ast.arg(arg=replacement or node.arg, annotation=None)
        return ast.copy_location(new, node)


def _bound_names(func: ast.AST) -> dict[str, str]:
    """Map every argument and locally-assigned name to a placeholder.

    Ordered by first appearance so two copies that renamed in step still
    normalise to the same thing. Arguments become ``a0…``, other bindings
    ``v0…``.
    """
    mapping: dict[str, str] = {}
    args = getattr(func, "args", None)
    if args is not None:
        ordered = [
            *getattr(args, "posonlyargs", []),
            *args.args,
            *args.kwonlyargs,
        ]
        if args.vararg:
            ordered.append(args.vararg)
        if args.kwarg:
            ordered.append(args.kwarg)
        for arg in ordered:
            mapping.setdefault(arg.arg, f"a{len(mapping)}")

    locals_seen = 0
    for node in ast.walk(func):
        names: list[str] = []
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names = [node.id]
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            continue
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names = [node.name]
        for name in names:
            if name not in mapping:
                mapping[name] = f"v{locals_seen}"
                locals_seen += 1
    return mapping


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _count_statements(body: list[ast.stmt]) -> int:
    return sum(1 for stmt in body for node in ast.walk(stmt) if isinstance(node, ast.stmt))


def normalize_function(func: ast.AST, path: Path, qualname: str) -> Function | None:
    """Normalise *func* into a :class:`Function`, or None if it has no body."""
    body = _strip_docstring(list(getattr(func, "body", [])))
    if not body:
        return None

    mapping = _bound_names(func)
    statement_hashes = []
    for stmt in body:
        clone = _Normalizer(mapping).visit(ast.parse(ast.unparse(stmt)).body[0])
        statement_hashes.append(
            _digest(ast.dump(clone, annotate_fields=False, include_attributes=False))
        )

    return Function(
        path=path,
        qualname=qualname,
        lineno=getattr(func, "lineno", 0),
        statements=_count_statements(body),
        body_hash=_digest("|".join(statement_hashes)),
        statement_hashes=statement_hashes,
    )


def iter_python_files(root: Path) -> list[Path]:
    """Every ``*.py`` under *root*, skipping the directories in SKIP_DIRS."""
    if root.is_file():
        return [root] if root.suffix == ".py" else []
    found: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        # Only the parts *below* root are ours to judge — this checkout may
        # itself live under a skipped name (an agent worktree lives under
        # .claude/worktrees/), and that must not exclude the whole scan.
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        found.append(path)
    return found


def collect(root: Path, min_statements: int) -> tuple[list[Function], list[str]]:
    """Normalise every function under *root*. Returns (functions, parse errors)."""
    functions: list[Function] = []
    errors: list[str] = []
    for path in iter_python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        for func, qualname in _iter_functions(tree):
            normalized = normalize_function(func, path, qualname)
            if normalized is not None and normalized.statements >= min_statements:
                functions.append(normalized)
    return functions, errors


def _iter_functions(tree: ast.AST, prefix: str = ""):
    """Yield ``(node, qualname)`` for every def, including nested and methods."""
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = f"{prefix}{node.name}"
            yield node, qualname
            yield from _iter_functions(node, prefix=f"{qualname}.")
        elif isinstance(node, ast.ClassDef):
            yield from _iter_functions(node, prefix=f"{prefix}{node.name}.")


def identical_groups(functions: list[Function]) -> list[list[Function]]:
    """Functions whose whole normalised body hashes the same, grouped."""
    buckets: dict[str, list[Function]] = defaultdict(list)
    for func in functions:
        buckets[func.body_hash].append(func)
    groups = [g for g in buckets.values() if len(g) > 1]
    groups.sort(key=lambda g: (-g[0].statements, g[0].where))
    return groups


def overlap(a: Function, b: Function) -> float:
    """Multiset Jaccard over statement hashes: |A∩B| / |A∪B|."""
    counts_a: dict[str, int] = defaultdict(int)
    for h in a.statement_hashes:
        counts_a[h] += 1
    counts_b: dict[str, int] = defaultdict(int)
    for h in b.statement_hashes:
        counts_b[h] += 1
    keys = set(counts_a) | set(counts_b)
    intersection = sum(min(counts_a[k], counts_b[k]) for k in keys)
    union = sum(max(counts_a[k], counts_b[k]) for k in keys)
    return intersection / union if union else 0.0


def near_duplicate_pairs(
    functions: list[Function],
    threshold: float,
) -> list[tuple[float, Function, Function]]:
    """Pairs scoring at or above *threshold*, excluding exact duplicates.

    Only pairs that share at least one statement hash are scored, which keeps
    this linear-ish in practice instead of quadratic over every function.
    """
    by_statement: dict[str, list[int]] = defaultdict(list)
    for index, func in enumerate(functions):
        for h in set(func.statement_hashes):
            by_statement[h].append(index)

    candidates: set[tuple[int, int]] = set()
    for indexes in by_statement.values():
        if len(indexes) < 2 or len(indexes) > 200:
            # A statement shared by hundreds of functions (`return None`) says
            # nothing about any particular pair.
            continue
        for i, left in enumerate(indexes):
            for right in indexes[i + 1 :]:
                candidates.add((left, right))

    scored: list[tuple[float, Function, Function]] = []
    for left, right in candidates:
        a, b = functions[left], functions[right]
        if a.body_hash == b.body_hash:
            continue  # reported as an identical group
        score = overlap(a, b)
        if score >= threshold:
            scored.append((score, a, b))
    scored.sort(key=lambda item: (-item[0], item[1].where))
    return scored


def report(
    functions: list[Function],
    groups: list[list[Function]],
    pairs: list[tuple[float, Function, Function]],
    errors: list[str],
    *,
    top: int,
) -> str:
    lines: list[str] = []
    for message in errors:
        lines.append(f"skipped (parse error): {message}")

    if groups:
        total = sum(len(g) for g in groups)
        lines.append(f"Identical function bodies — {len(groups)} group(s), {total} function(s):")
        for group in groups[:top]:
            lines.append(
                f"  [{group[0].statements} stmts] {group[0].qualname}() and {len(group) - 1} more:"
            )
            for func in group:
                lines.append(f"    {func.where}  {func.qualname}()")
        if len(groups) > top:
            lines.append(f"  … {len(groups) - top} more group(s) not shown")
    else:
        lines.append("Identical function bodies: none")

    if pairs:
        lines.append("")
        lines.append(f"High-overlap pairs — {len(pairs)}:")
        for score, a, b in pairs[:top]:
            lines.append(f"  {score:.0%}  {a.where} {a.qualname}()")
            lines.append(f"        {b.where} {b.qualname}()")
        if len(pairs) > top:
            lines.append(f"  … {len(pairs) - top} more pair(s) not shown")
    else:
        lines.append("")
        lines.append("High-overlap pairs: none")

    lines.append("")
    lines.append(f"{len(functions)} function(s) considered.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_duplicates.py",
        description="Report duplicate and near-duplicate Python function bodies.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=REPO_ROOT,
        help="file or directory to scan (default: the repo root)",
    )
    parser.add_argument(
        "--min-statements",
        type=int,
        default=DEFAULT_MIN_STATEMENTS,
        help=f"ignore functions shorter than this (default: {DEFAULT_MIN_STATEMENTS})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"overlap ratio to report a pair (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"how many groups/pairs to print (default: {DEFAULT_TOP})",
    )
    parser.add_argument(
        "--fail-on-identical",
        action="store_true",
        help="exit 1 when identical bodies are found (default: report only)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print only the summary counts",
    )
    args = parser.parse_args(argv)

    root = args.path.resolve()
    if not root.exists():
        print(f"check_duplicates: no such path: {root}", file=sys.stderr)
        return 2
    if not 0.0 < args.threshold <= 1.0:
        print("check_duplicates: --threshold must be in (0, 1]", file=sys.stderr)
        return 2
    if args.min_statements < 1:
        print("check_duplicates: --min-statements must be >= 1", file=sys.stderr)
        return 2

    functions, errors = collect(root, args.min_statements)
    groups = identical_groups(functions)
    pairs = near_duplicate_pairs(functions, args.threshold)

    if args.quiet:
        print(
            f"check_duplicates: {len(groups)} identical group(s), "
            f"{len(pairs)} high-overlap pair(s), "
            f"{len(functions)} function(s) considered"
        )
    else:
        print(report(functions, groups, pairs, errors, top=args.top))

    return 1 if (groups and args.fail_on_identical) else 0


if __name__ == "__main__":
    raise SystemExit(main())
