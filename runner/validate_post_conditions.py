"""Literal-sanity checks for plan post-conditions (T-2026-313).

A ``file_contains`` post-condition is a literal substring the finished file
must contain; the executor checks it with plain ``text in body``. The
2026-09-04 night's plan demanded
``from core.testing import import_placeholder_never_used`` — a placeholder
the planner never replaced — and no implementation could satisfy it, so the
step (and the night) was lost at plan time while the plan itself validated.

Two cheap checks catch that class where the planner's retry loop can fix it
for free:

* **placeholder tokens** — ``placeholder``, ``never_used``, ``lorem ipsum``
  and the whole words ``TODO`` / ``FIXME`` / ``TBD`` / ``XXX`` in a
  ``file_contains`` text. (``file_lacks`` is exempt: asserting the absence
  of a TODO is legitimate.)
* **unresolvable imports** — a text that parses as ``from M import N``
  where ``M`` is a module of the target repo must name a module that exists
  (or that a plan step creates) and names that the module defines (unless a
  plan step modifies the module, in which case the plan is trusted).
  Stdlib and third-party modules are not checked; plain ``import M`` is not
  checked either, since a bare module name says nothing about a symbol.

Library module: ``runner/validate_plan.py`` calls ``check_literals`` with
the resolved target repo root.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path
from typing import Optional

# ``placeholder`` followed by ``=`` is the HTML/GUI attribute (``placeholder="Search"``),
# a legitimate anchor in gui/web edits, not a planner stand-in.
PLACEHOLDER_TOKENS = re.compile(
    r"placeholder(?!\s*=\s*[\"'])|never_used|lorem ipsum|\b(?:TODO|FIXME|TBD|XXX)\b",
    re.IGNORECASE,
)


def check_literals(steps: list, repo_root: Path) -> list:
    """Return error strings for placeholder or unresolvable ``file_contains`` texts."""
    errors: list = []
    planned = planned_paths(steps)
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        conds = step.get("post_conditions")
        if not isinstance(conds, list):
            continue
        for j, cond in enumerate(conds):
            if not isinstance(cond, dict) or cond.get("type") != "file_contains":
                continue
            text = cond.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            label = f"step[{i}].post_conditions[{j}]"
            found = PLACEHOLDER_TOKENS.search(text)
            if found:
                errors.append(
                    f"{label}: file_contains text {text!r} looks like a placeholder "
                    f"({found.group(0)!r}). The text must be the literal that the "
                    f"finished file will contain; pick an anchor the step actually writes."
                )
                continue
            cond_file = cond.get("file") if isinstance(cond.get("file"), str) else ""
            errors.extend(check_import_literal(label, cond_file, text, planned, repo_root))
    return errors


def planned_paths(steps: list) -> set:
    """Repo-relative POSIX paths every step declares in ``files``."""
    out: set = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        for f in step.get("files") or []:
            if isinstance(f, str) and f.strip():
                out.add(_norm(f))
    return out


def _norm(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("./")


def check_import_literal(
    label: str, cond_file: str, text: str, planned: set, repo_root: Path
) -> list:
    """Errors for a text that parses as ``from M import N`` against the repo."""
    try:
        tree = ast.parse(textwrap.dedent(text))
    except (SyntaxError, ValueError):
        return []
    errors: list = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        rel = resolve_module_path(node, cond_file, repo_root)
        if rel is None:
            continue
        shown = ("." * node.level) + (node.module or "")
        if rel in planned:
            continue
        target = repo_root / rel
        if not target.is_file():
            errors.append(
                f"{label}: file_contains text imports {shown!r} but {rel} does not "
                f"exist in the repo and no step creates it"
            )
            continue
        defined = top_level_names(target)
        if defined is None:
            continue
        package_dir = target.parent if target.name == "__init__.py" else None
        for alias in node.names:
            if alias.name == "*":
                continue
            if package_dir is not None and (
                (package_dir / f"{alias.name}.py").is_file()
                or (package_dir / alias.name / "__init__.py").is_file()
            ):
                continue  # ``from package import submodule``
            if alias.name not in defined:
                errors.append(
                    f"{label}: file_contains text imports {alias.name!r} from {shown!r} "
                    f"but {rel} defines no such name and no step modifies it"
                )
    return errors


def resolve_module_path(node: ast.ImportFrom, cond_file: str, repo_root: Path) -> Optional[str]:
    """Repo-relative path the import refers to, or None when it is not a
    repo module (stdlib, third-party) or cannot be anchored.

    Absolute imports count as repo modules only when their first segment is
    a directory or ``.py`` file at the repo root. Relative imports resolve
    against the post-condition's own ``file``. Prefers ``<mod>.py``, then
    ``<mod>/__init__.py``; falls back to ``<mod>.py`` when neither exists so
    the caller can report it missing.
    """
    parts = (node.module or "").split(".") if node.module else []
    if node.level == 0:
        if not parts:
            return None
        head = repo_root / parts[0]
        if not (head.is_dir() or head.with_suffix(".py").is_file()):
            return None
        base = ""
    else:
        if not cond_file:
            return None
        base_parts = _norm(cond_file).split("/")[:-1]
        up = node.level - 1
        if up > len(base_parts):
            return None
        base_parts = base_parts[: len(base_parts) - up] if up else base_parts
        base = "/".join(base_parts)
    joined = "/".join(parts)
    stem = "/".join(p for p in (base, joined) if p)
    if not stem:
        return None
    candidates = [f"{stem}.py", f"{stem}/__init__.py"]
    for cand in candidates:
        if (repo_root / cand).is_file():
            return cand
    return candidates[0]


_CONTAINERS = (ast.If, ast.Try, ast.With)


def top_level_names(path: Path) -> Optional[set]:
    """Names a module defines or re-exports at top level (including inside
    top-level ``if`` / ``try`` / ``with`` blocks, where guarded imports live).
    None when the file cannot be parsed."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None
    names: set = set()

    def visit(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    _collect_targets(target, names)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                _collect_targets(node.target, names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
            elif isinstance(node, _CONTAINERS):
                visit(getattr(node, "body", []))
                visit(getattr(node, "orelse", []))
                for handler in getattr(node, "handlers", []) or []:
                    visit(handler.body)
                visit(getattr(node, "finalbody", []))

    visit(tree.body)
    return names


def _collect_targets(target, names: set) -> None:
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _collect_targets(elt, names)
