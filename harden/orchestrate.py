#!/usr/bin/env python3
"""Harden orchestrator — the /harden control flow, in Python instead of prose.

``harden/commands/harden.md`` used to carry the whole attack-defend loop as
instructions: path selection, directory expansion, the pre-flight size check,
the cost formula, worktree lifecycle, prompt assembly, the
validate/retry/degrade policy, the budget abort threshold and TODO filing.
That is a program, and a program belongs in code (review X-8, plan row 3.8).

This module owns all of it. The skill now does three things: call a subcommand
here, spawn the agents with the prompts this prints, and relay the results.

Every subcommand prints either a human-readable block (``plan``, ``prompt``,
``worktree``) or a single JSON decision object (``validate``, ``budget``,
``file-todos``) that tells the caller exactly what to do next.

Usage::

    orchestrate.py plan --session-id ID --targets src/a.py src/b.py [flags]
    orchestrate.py plan --session-id ID --plan-note 42
    orchestrate.py prompt attacker --session-id ID --round 1
    orchestrate.py prompt consolidator --session-id ID --round 1 --findings F.json
    orchestrate.py prompt defender --session-id ID --round 1 --findings F.json
    orchestrate.py prompt defender-continue --session-id ID --round 2 --findings F.json
    orchestrate.py worktree check|create|remove|diff --session-id ID
    orchestrate.py round start|tick|status|reset|defender --session-id ID
    orchestrate.py validate findings|response|consolidation --file OUT.json --attempt N
    orchestrate.py budget check --session-id ID --spent N
    orchestrate.py file-todos --session-id ID --round N --response R.json
    orchestrate.py save-summary --session-id ID --content-file S.md

Exit codes: ``0`` success, ``1`` abort/hard error (message on stderr),
``3`` ``validate`` rejected the agent output (decision JSON on stdout).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import lenses as lens_taxonomy  # noqa: E402
import round_counter  # noqa: E402

APIARY_REPO = HERE.parent
AGENTS_DIR = HERE / "agents"
VALIDATE_AND_ASSIGN = HERE / "validate_and_assign.py"
QUERY_REQUEST = APIARY_REPO / "budgeter" / "query_request.py"

# --------------------------------------------------------------------------- #
# Constants that used to live as numbers in the skill's prose
# --------------------------------------------------------------------------- #

DEFAULT_ROUNDS = 3
DEFAULT_MAX_FILES = 5
DEFAULT_MODEL = "sonnet"
DEFAULT_BUDGET_TOKENS = 450_000
DEFAULT_MAX_TARGET_KB = 50
DEFAULT_FOCUS = "general"

VALID_FOCUS = ("general", "security", "input", "logic", "complexity", "resilience")

# Cost model (scribe note C-2026-24; multi-lens fan-out per C-2026-48).
BASE_TOKENS_PER_CALL = 15_000
TOKENS_PER_KB_PER_CALL = 1.5 * 256  # ~384

# One retry, then the per-kind fallback. The prose said "once, then ask/degrade";
# encoding it here means a validator that keeps failing cannot loop forever.
MAX_VALIDATION_ATTEMPTS = 2

CODE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".sh")
EXCLUDED_DIR_NAMES = frozenset({"__pycache__", "node_modules", ".git"})
TEST_FILE_GLOBS = ("test_*.py", "*_test.py", "*_test.go",
                   "*.test.ts", "*.test.js", "*.spec.ts", "*.spec.js")

RETURN_JSON_ARRAY_RULE = (
    "Return ONLY a raw JSON array. No markdown fences, no explanation. Just the JSON."
)
ORIGINAL_PATHS_RULE = (
    "In your findings, always use the ORIGINAL relative file paths "
    "(e.g. `src/app.py:45-50`), not the worktree paths. The location field must "
    "match the original project structure."
)
DEFENDER_PATHS_RULE = (
    "Read and edit files at the worktree paths provided. In your JSON response, use "
    "the ORIGINAL relative file paths (e.g. `src/app.py`) in the `changes.file` field, "
    "not the worktree paths."
)
DEFENDER_WORKFLOW_RULE = (
    "WORKFLOW: First, use the Read tool to read each target file. Then use the Edit "
    "tool to make your fixes — this is required, do not skip it. After all edits are "
    "complete, return your JSON summary. The JSON documents what you already changed, "
    "it is not a plan."
)

INTEGER_RE = re.compile(r"^[0-9]+$")
FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


class Abort(Exception):
    """A user-facing refusal: print the message and stop the run."""


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #

def _safe_name(session_id: str) -> str:
    return session_id.replace("/", "-").replace("\\", "-")


def _tmp_dir() -> Path:
    """The harden scratch dir (``HARDEN_TMP_DIR`` or ``harden/tmp``)."""
    return round_counter.TMP_DIR


def _plan_path(session_id: str) -> Path:
    return _tmp_dir() / f"plan_{_safe_name(session_id)}.json"


def _read_json_file(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Abort(f"{what} not found: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        raise Abort(f"{what} is unreadable ({exc}): {path}")


def strip_fences(text: str) -> str:
    """Remove a single wrapping markdown fence, if present.

    Agents are told to return raw JSON and routinely wrap it anyway. Stripping
    here means the skill never has to describe how to do it.
    """
    match = FENCE_RE.match(text)
    return match.group(1) if match else text.strip()


def _run_git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def _repo_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise Abort("not inside a git repository — /harden needs a git worktree")
    return Path(result.stdout.strip()).resolve()


def _default_launcher(repo: Path) -> Path:
    return repo / ".claude" / "apiary" / "launch.py"


# --------------------------------------------------------------------------- #
# `plan` — path selection, target resolution, size check, cost estimate
# --------------------------------------------------------------------------- #

def _is_excluded_dir(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return any(part in EXCLUDED_DIR_NAMES for part in parts)


def _is_test_file(path: Path) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(path.name, pattern) for pattern in TEST_FILE_GLOBS)


def expand_targets(raw_targets: list[str], cwd: Path) -> tuple[list[Path], list[str]]:
    """Expand directory arguments into code files, deduped, order preserved.

    Returns ``(paths, empty_dirs)``. Exclusions (``__pycache__``, ``node_modules``,
    ``.git``, test files) are applied to *directory expansion only* — a file the
    user named explicitly is always kept, which is the safer reading of the
    prose (it listed the exclusions under "Expand directories").
    """
    resolved: list[Path] = []
    seen: set[Path] = set()
    empty_dirs: list[str] = []

    for raw in raw_targets:
        candidate = (cwd / raw) if not Path(raw).is_absolute() else Path(raw)
        if candidate.is_dir():
            found = []
            for path in sorted(candidate.rglob("*")):
                if not path.is_file() or path.suffix not in CODE_SUFFIXES:
                    continue
                if _is_excluded_dir(path, candidate) or _is_test_file(path):
                    continue
                found.append(path)
            if not found:
                empty_dirs.append(raw)
            for path in found:
                key = path.resolve()
                if key not in seen:
                    seen.add(key)
                    resolved.append(path)
        else:
            key = candidate.resolve()
            if key not in seen:
                seen.add(key)
                resolved.append(candidate)

    return resolved, empty_dirs


def resolve_lenses(lenses_arg: str | None) -> list[str]:
    """Parse/validate ``--lenses``; default to the full canonical taxonomy."""
    if not lenses_arg:
        return lens_taxonomy.all_lenses()
    out: list[str] = []
    for entry in lenses_arg.split(","):
        name = entry.strip().lower()
        if not name:
            continue
        if not lens_taxonomy.is_valid_lens(name):
            raise Abort(
                f"Unknown lens `{name}`. Valid lenses: "
                + ", ".join(lens_taxonomy.all_lenses())
                + "."
            )
        if name not in out:
            out.append(name)
    if not out:
        raise Abort(
            "`--lenses` was empty. Valid lenses: "
            + ", ".join(lens_taxonomy.all_lenses())
            + "."
        )
    return out


def select_path(mode: str, focus_explicit: bool, lenses_given: bool,
                resolved_lenses: list[str]) -> str:
    """Decide which Step 2 loop runs: legacy, single-lens, or multi-lens."""
    if mode == "plan":
        return "legacy"
    if focus_explicit and not lenses_given:
        return "legacy"
    return "single-lens" if len(resolved_lenses) == 1 else "multi-lens"


def estimate_tokens(total_kb: int, rounds: int, path: str, lens_count: int) -> dict:
    per_call = BASE_TOKENS_PER_CALL + TOKENS_PER_KB_PER_CALL * total_kb
    calls_per_round = lens_count + 2 if path == "multi-lens" else 2
    return {
        "per_call_tokens": int(per_call),
        "calls_per_round": calls_per_round,
        "estimated_tokens": int(rounds * calls_per_round * per_call),
    }


def make_request_id(session_id: str, now: int | None = None) -> str:
    """``harden-<sid8>-<unix_ts>-<rand4>``. Pure string computation, no side effect."""
    stamp = int(time.time()) if now is None else now
    return f"harden-{session_id[:8]}-{stamp}-{secrets.token_hex(2)}"


def _render_summary(plan: dict) -> str:
    lens_line = ", ".join(plan["resolved_lenses"]) if plan["path"] != "legacy" else "—"
    lines = [
        "**Harden configuration:**",
        f"- Mode: {plan['mode']}",
        f"- Path: {plan['path']}",
        f"- Target: {plan['target_label']}",
        f"- Lenses: {lens_line}",
        f"- Focus: {plan['focus'] if plan['path'] == 'legacy' else '—'}",
        f"- Deep mode: {'yes' if plan['deep'] else 'no'}",
        f"- Max rounds: {plan['rounds']}",
        f"- Attacker model: {plan['models']['attacker']}",
    ]
    if plan["path"] == "multi-lens":
        lines.append(f"- Consolidator model: {plan['models']['consolidator']}")
    lines += [
        f"- Defender model: {plan['models']['defender']}",
        f"- Estimated cost: ~{plan['estimated_tokens']} tokens",
        f"- Token budget: {plan['budget_tokens']}",
    ]
    if plan["budget_warning"]:
        lines = [plan["budget_warning"], ""] + lines
    return "\n".join(lines)


def cmd_plan(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    repo = _repo_root(args.repo)
    mode = "plan" if args.plan_note else "code"
    notes: list[str] = []

    if mode == "code" and not args.targets:
        raise Abort("No target given. Pass one or more file paths, a directory, "
                    "or `--plan <note-id>`.")
    if mode == "plan" and args.targets:
        notes.append("plan mode: positional targets ignored (the note is the target)")

    focus_explicit = args.focus is not None
    focus = args.focus or DEFAULT_FOCUS
    lenses_given = args.lenses is not None
    resolved_lenses = resolve_lenses(args.lenses) if mode == "code" else []
    if mode == "plan" and lenses_given:
        notes.append("plan mode: --lenses ignored (lenses do not apply to plans)")

    path = select_path(mode, focus_explicit, lenses_given, resolved_lenses)
    if path == "legacy":
        resolved_lenses = []

    targets: list[Path] = []
    targets_rel: list[str] = []
    note_content = ""
    total_bytes = 0

    if mode == "code":
        targets, empty_dirs = expand_targets(args.targets, cwd)
        if not targets:
            if empty_dirs:
                raise Abort(f"No code files found in `{empty_dirs[0]}`. "
                            "Check the path or add files explicitly.")
            raise Abort("No target files after expansion. Pass specific files.")
        if len(targets) > args.max_files:
            raise Abort(f"Too many files ({len(targets)} > {args.max_files}). "
                        "Narrow scope, use `--max-files N`, or pass specific files "
                        "instead of a directory.")
        missing = [str(p) for p in targets if not p.is_file()]
        if missing:
            raise Abort("Target file(s) not found:\n" + "\n".join(f"  - {m}" for m in missing))

        try:
            total_bytes = sum(p.stat().st_size for p in targets)
        except OSError as exc:
            raise Abort(f"Pre-flight size check failed: {exc}")
        total_kb = math.ceil(total_bytes / 1024)
        if total_kb > args.max_target_kb:
            raise Abort(f"Target size {total_kb} KB exceeds --max-target-kb "
                        f"{args.max_target_kb}. Narrow scope or raise the cap.")

        for p in targets:
            try:
                targets_rel.append(p.resolve().relative_to(repo).as_posix())
            except ValueError:
                targets_rel.append(p.as_posix())
        target_label = ", ".join(targets_rel)
    else:
        note_content = _fetch_note(args.plan_note, repo, args.launcher)
        # Plan mode skips the pre-flight size check (there are no files to stat),
        # but the cost formula still needs a target size — use the note's own
        # length. The prose said "reuse total_kb" without defining it for plan
        # mode; this is the reading that keeps the estimate honest.
        total_bytes = len(note_content.encode("utf-8"))
        total_kb = math.ceil(total_bytes / 1024)
        notes.append("plan mode: pre-flight size check skipped; cost estimated "
                     "from the note's own size")
        target_label = f"note #{args.plan_note}"

    estimate = estimate_tokens(total_kb, args.rounds, path, len(resolved_lenses))
    budget_warning = None
    if estimate["estimated_tokens"] > args.budget_tokens:
        budget_warning = (f"WARNING: Estimated cost ({estimate['estimated_tokens']}) exceeds "
                          f"budget ({args.budget_tokens}). Forcing through will hard-abort "
                          "mid-run on overrun.")

    safe = _safe_name(args.session_id)
    plan = {
        "session_id": args.session_id,
        "session_cwd": str(cwd),
        "repo_root": str(repo),
        "request_id": args.request_id or make_request_id(args.session_id),
        "mode": mode,
        "path": path,
        "resolved_lenses": resolved_lenses,
        "lens_codes": {name: lens_taxonomy.code_for(name) for name in resolved_lenses},
        "focus": focus,
        "focus_explicit": focus_explicit,
        "deep": bool(args.deep),
        "rounds": args.rounds,
        "max_files": args.max_files,
        "max_target_kb": args.max_target_kb,
        "models": {
            "attacker": args.model_attacker,
            "consolidator": args.model_consolidator,
            "defender": args.model_defender,
        },
        "budget_tokens": args.budget_tokens,
        "targets": [str(p) for p in targets],
        "targets_rel": targets_rel,
        "target_label": target_label,
        "plan_note_id": args.plan_note,
        "plan_note_content": note_content,
        "total_bytes": total_bytes,
        "total_kb": total_kb,
        "budget_warning": budget_warning,
        "worktree_path": f".claude/worktrees/harden-{safe}" if mode == "code" else None,
        "worktree_branch": f"harden-{safe}" if mode == "code" else None,
        "notes": notes,
        **estimate,
    }
    plan["summary"] = _render_summary(plan)

    out_path = Path(args.out) if args.out else _plan_path(args.session_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        print(plan["summary"])
        for note in notes:
            print(f"note: {note}")
        print(f"\nPlan written to {out_path}")
        print("Nothing has been created yet — ask the user to confirm in plain prose, "
              "then run `worktree check`, `round start`, `worktree create`.")
    return 0


def _fetch_note(note_id: str, repo: Path, launcher: str | None) -> str:
    launch = Path(launcher) if launcher else _default_launcher(repo)
    if not launch.is_file():
        raise Abort(f"apiary launcher not found at {launch} — re-run `apiary install`.")
    result = subprocess.run(
        [sys.executable, str(launch), "scribe/notes.py", "get", str(note_id)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise Abort(f"Note {note_id} not found. Use `/notes` to find the correct ID.")
    return result.stdout.strip()


def load_plan(session_id: str | None, plan_file: str | None) -> dict:
    if plan_file:
        return _read_json_file(Path(plan_file), "plan file")
    if not session_id:
        raise Abort("pass --session-id (or --plan-file) so the run's plan can be loaded")
    path = _plan_path(session_id)
    if not path.is_file():
        raise Abort(f"no plan for session {session_id} at {path} — run "
                    "`orchestrate.py plan ...` first")
    return _read_json_file(path, "plan file")


# --------------------------------------------------------------------------- #
# `worktree` — readiness check, create, remove, diff
# --------------------------------------------------------------------------- #

def _dirty_targets(plan: dict, repo: Path) -> list[str]:
    dirty = []
    for rel in plan["targets_rel"]:
        result = _run_git(["status", "--porcelain", "--", rel], repo)
        if result.returncode != 0:
            raise Abort(f"git status failed for {rel}: {result.stderr.strip()}")
        if result.stdout.strip():
            dirty.append(rel)
    return dirty


def cmd_worktree(args: argparse.Namespace) -> int:
    plan = load_plan(args.session_id, args.plan_file)
    repo = Path(args.repo).resolve() if args.repo else Path(plan["repo_root"])

    if plan["mode"] == "plan":
        print("plan mode: no worktree is used — skip this step.")
        return 0

    wt_rel = plan["worktree_path"]
    wt_abs = repo / wt_rel
    branch = plan["worktree_branch"]

    if args.action in ("check", "create"):
        dirty = _dirty_targets(plan, repo)
        if dirty:
            raise Abort(
                f"Target {dirty[0]} has uncommitted changes or is untracked. "
                "Commit or stash it first — /harden operates on a worktree created "
                "from HEAD."
            )
        if args.action == "check":
            print(f"{len(plan['targets_rel'])} target(s) clean at HEAD — safe to create "
                  "the worktree.")
            return 0

    if args.action == "create":
        result = _run_git(["worktree", "add", wt_rel, "-b", branch, "HEAD"], repo)
        if result.returncode != 0:
            raise Abort(f"git worktree add failed: {result.stderr.strip()}")
        print(f"worktree_path: {wt_rel}")
        print(f"worktree_branch: {branch}")
        return 0

    if args.action == "remove":
        result = _run_git(["worktree", "remove", wt_rel], repo)
        if result.returncode != 0:
            raise Abort(f"git worktree remove failed: {result.stderr.strip()}")
        print(f"removed worktree {wt_rel}")
        if args.delete_branch:
            branch_result = _run_git(["branch", "-D", branch], repo)
            if branch_result.returncode != 0:
                print(f"warning: could not delete branch {branch}: "
                      f"{branch_result.stderr.strip()}", file=sys.stderr)
            else:
                print(f"deleted branch {branch}")
        else:
            print(f"branch {branch} kept (pass --delete-branch to remove it too)")
        return 0

    # diff
    if not wt_abs.is_dir():
        raise Abort(f"worktree {wt_rel} does not exist")
    result = _run_git(["diff", "HEAD"], wt_abs)
    if result.returncode != 0:
        raise Abort(f"git diff failed: {result.stderr.strip()}")
    if not result.stdout.strip():
        print("Defenders did not make any file edits. No code changes to review.")
        return 0
    print(result.stdout, end="")
    return 0


# --------------------------------------------------------------------------- #
# `round` — thin wrapper over round_counter
# --------------------------------------------------------------------------- #

def cmd_round(args: argparse.Namespace) -> int:
    if args.action == "defender":
        if not args.set_id and not args.get:
            raise Abort("round defender needs --set <agent_id> or --get")
        round_counter.cmd_defender(args.session_id, set_id=args.set_id)
        return 0
    {
        "start": round_counter.cmd_start,
        "tick": round_counter.cmd_tick,
        "reset": round_counter.cmd_reset,
        "status": round_counter.cmd_status,
    }[args.action](args.session_id)
    return 0


# --------------------------------------------------------------------------- #
# `prompt` — assemble agent prompts from the templates
# --------------------------------------------------------------------------- #

def _template(name: str) -> str:
    path = AGENTS_DIR / name
    if not path.is_file():
        raise Abort(f"agent template missing: {path}")
    return path.read_text(encoding="utf-8")


def _fill(template: str, mapping: dict) -> str:
    for key, value in mapping.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def _target_block(plan: dict, round_no: int, for_defender: bool) -> str:
    """The ``{{TARGET_CONTENT}}`` body: worktree paths where cumulative edits live."""
    if plan["mode"] == "plan":
        return plan["plan_note_content"]

    use_worktree = for_defender or round_no > 1
    prefix = plan["worktree_path"] + "/" if use_worktree else ""
    listing = "\n".join(f"- {prefix}{rel}" for rel in plan["targets_rel"])
    if for_defender:
        head = "Read and edit these files with the Read and Edit tools:"
        rule = DEFENDER_PATHS_RULE
    else:
        head = "Read these files with the Read tool:"
        rule = ORIGINAL_PATHS_RULE
    return f"{head}\n{listing}\n\n{rule}"


def build_prior_record(prev_findings, prev_response, rejections, round_no: int) -> str:
    """Compact, mechanical record of the previous round — never LLM recall."""
    if not prev_findings and not prev_response and not rejections:
        return "None (first round)"

    outcome: dict[str, tuple[str, str]] = {}
    for item in (prev_response or {}).get("responses", []):
        ref = item.get("finding_ref")
        if isinstance(ref, str):
            outcome[ref] = (item.get("action", "?"), item.get("description", ""))

    lines = [f"Round {max(round_no - 1, 1)}:"]
    for finding in prev_findings or []:
        fid = finding.get("id", "?")
        loc = finding.get("location", "?")
        action = outcome.get(fid, ("not addressed", ""))[0]
        lines.append(f"- {fid} {loc} — {action}")

    rejected = (rejections or {}).get("rejected", []) if isinstance(rejections, dict) else []
    if rejected:
        lines.append("Referee rejections:")
        for item in rejected:
            # Rejections key on `source_ids` (the ATK ids that were dropped);
            # `id` is only present on degraded/legacy payloads.
            ids = item.get("source_ids") or ([item["id"]] if item.get("id") else ["?"])
            label = ", ".join(str(i) for i in ids)
            lines.append(f"- {label} — {item.get('reason', 'no reason given')}")
    return "\n".join(lines)


def _emit_agent(description: str, model: str, prompt: str) -> None:
    print(f"AGENT description: {description}")
    print(f"AGENT model: {model}")
    print("AGENT subagent_type: general-purpose")
    print("--- PROMPT BEGIN ---")
    print(prompt)
    print("--- PROMPT END ---")


def _load_optional(path: str | None, what: str):
    return _read_json_file(Path(path), what) if path else None


def cmd_prompt(args: argparse.Namespace) -> int:
    plan = load_plan(args.session_id, args.plan_file)
    rid = plan["request_id"]
    round_no = args.round
    prev_findings = _load_optional(args.prev_findings, "previous findings")
    prev_response = _load_optional(args.prev_response, "previous defender response")
    rejections = _load_optional(args.rejections, "consolidation output")

    if args.role == "attacker":
        if plan["path"] == "legacy":
            prev = (json.dumps(prev_response, indent=2) if prev_response
                    else "None (first round)")
            prompt = _fill(_template("attacker.md"), {
                "MODE": plan["mode"],
                "FOCUS": plan["focus"],
                "DEEP": "true" if plan["deep"] else "false",
                "PREV_DEFENDER": prev,
                "TARGET_CONTENT": _target_block(plan, round_no, for_defender=False),
            }) + "\n\n" + RETURN_JSON_ARRAY_RULE
            _emit_agent(f"Harden Attacker round {round_no} [rid:{rid}]",
                        plan["models"]["attacker"], prompt)
            return 0

        taxonomy = {
            "briefs": lens_taxonomy.LENS_BRIEFS,
            "seam_rules": lens_taxonomy.SEAM_RULES,
        }
        wanted = args.lens or plan["resolved_lenses"]
        unknown = [name for name in wanted if not lens_taxonomy.is_valid_lens(name)]
        if unknown:
            raise Abort(f"Unknown lens `{unknown[0]}`. Valid lenses: "
                        + ", ".join(lens_taxonomy.all_lenses()) + ".")
        prior = build_prior_record(prev_findings, prev_response, rejections, round_no)
        template = _template("attacker_lens.md")
        for index, name in enumerate(wanted):
            prompt = _fill(template, {
                "MODE": "code",
                "LENS": name,
                "LENS_BRIEF": taxonomy["briefs"][name],
                "SEAM_RULES": taxonomy["seam_rules"],
                "DEEP": "true" if plan["deep"] else "false",
                "PREV_ROUND": prior,
                "TARGET_CONTENT": _target_block(plan, round_no, for_defender=False),
            }) + "\n\n" + RETURN_JSON_ARRAY_RULE
            if index:
                print()
            _emit_agent(f"Harden Attacker {name} round {round_no} [rid:{rid}]",
                        plan["models"]["attacker"], prompt)
        print()
        print(f"Spawn all {len(wanted)} agent(s) in ONE message so they run in parallel. "
              "Do not pass `isolation` — attackers only Read.")
        return 0

    if not args.findings:
        raise Abort(f"prompt {args.role} needs --findings <validated findings json>")

    if args.role == "consolidator":
        findings = _read_json_file(Path(args.findings), "combined findings")
        prompt = _fill(_template("consolidator.md"), {
            "MODE": plan["mode"],
            "PRIOR_RECORD": build_prior_record(prev_findings, prev_response,
                                               rejections, round_no),
            "FINDINGS_JSON": json.dumps(findings, indent=2),
        })
        _emit_agent(f"Harden Consolidator round {round_no} [rid:{rid}]",
                    plan["models"]["consolidator"], prompt)
        return 0

    findings = _read_json_file(Path(args.findings), "validated findings")

    if args.role == "defender":
        prompt = _fill(_template("defender.md"), {
            "MODE": plan["mode"],
            "FINDINGS_JSON": json.dumps(findings, indent=2),
            "TARGET_CONTENT": _target_block(plan, round_no, for_defender=True),
        })
        if plan["mode"] == "code":
            prompt += "\n\n" + DEFENDER_WORKFLOW_RULE
        _emit_agent(f"Harden Defender round {round_no} [rid:{rid}]",
                    plan["models"]["defender"], prompt)
        print()
        print("Do NOT pass `isolation` — the Defender edits the shared worktree directly.")
        print("After it responds, store its agent id: "
              f"`orchestrate.py round defender --session-id {plan['session_id']} "
              "--set <agent_id>`")
        return 0

    # defender-continue: a SendMessage body, not an Agent spawn.
    fixed, deferred = [], []
    for item in (prev_response or {}).get("responses", []):
        ref = item.get("finding_ref", "?")
        if item.get("action") == "deferred":
            deferred.append(ref)
        else:
            fixed.append(ref)
    expected = [f.get("id", "?") for f in findings] if isinstance(findings, list) else []
    print("SENDMESSAGE to the stored Defender agent id "
          f"(`orchestrate.py round defender --session-id {plan['session_id']} --get`)")
    print("--- MESSAGE BEGIN ---")
    print(f"## Round {round_no} Findings\n")
    print(f"The reviewers re-examined your fixes and found {len(expected)} new issues.\n")
    print("### New findings")
    print(json.dumps(findings, indent=2))
    print("\n### Previous round summary")
    print(f"- Fixed: {len(fixed)} ({', '.join(fixed) or 'none'})")
    print(f"- Deferred: {len(deferred)} ({', '.join(deferred) or 'none'})")
    print("\nApply the same process: fix what you can in the worktree, defer what you "
          "can't, then return your JSON summary.")
    print("--- MESSAGE END ---")
    return 0


# --------------------------------------------------------------------------- #
# `validate` — validators + the retry/degrade policy
# --------------------------------------------------------------------------- #

RETRY_FEEDBACK = (
    "Your previous output failed validation:\n{errors}\n\n"
    "Return a corrected version. Raw JSON only — no markdown fences, no prose."
)

NEXT_ACTIONS = {
    "retry": ("Re-spawn the SAME agent once with the `feedback` text appended to its "
              "prompt, then run this command again with --attempt {next}."),
    "drop": ("Drop lens `{lens}` for this round: record "
             "\"lens {lens}: dropped (unparseable output)\" in the round summary and "
             "continue with the remaining lenses. Do not abort the round."),
    "ask": ("Show the errors and ask the user, in plain prose, whether to skip this "
            "round and continue, or stop now with partial results (Step 3). Do not "
            "use a multiple-choice picker."),
    "degrade": ("Run `orchestrate.py validate consolidation --degrade --file "
                "<combined-findings.json>` to build the consolidated set "
                "deterministically, and record \"consolidator: degraded (adjudication "
                "skipped)\" in the round summary."),
}


SEVERITIES = ("critical", "high", "medium", "low")
ACTIONS = ("fixed", "refactored", "deferred")


def tally(kind: str, validated) -> dict:
    """Mechanical counts for the round-summary lines — never LLM arithmetic."""
    if kind == "response":
        items = validated.get("responses", []) if isinstance(validated, dict) else []
        counts = {action: 0 for action in ACTIONS}
        for item in items:
            action = item.get("action")
            if action in counts:
                counts[action] += 1
        return {"total": len(items), **counts}

    if kind == "consolidation":
        accepted = validated.get("accepted", []) if isinstance(validated, dict) else []
        rejected = validated.get("rejected", []) if isinstance(validated, dict) else []
        counts = {"total": len(accepted), "rejected": len(rejected)}
    else:
        accepted = validated if isinstance(validated, list) else []
        counts = {"total": len(accepted)}

    for severity in SEVERITIES:
        counts[severity] = sum(1 for f in accepted if f.get("severity") == severity)
    return counts


def _fallback_action(kind: str, lens: str | None) -> str:
    if kind == "consolidation":
        return "degrade"
    if kind == "findings" and lens:
        return "drop"
    return "ask"


def _validator_argv(kind: str, temp: Path, args: argparse.Namespace,
                    check_files: bool, deep: bool) -> list[str]:
    argv = [sys.executable, str(VALIDATE_AND_ASSIGN), kind, "--file", str(temp)]
    if check_files:
        argv.append("--check-files")
    if kind == "findings":
        argv.append("--sanitize")
        if deep:
            argv.append("--deep")
        if args.lens:
            argv += ["--lens", args.lens]
    elif kind == "response":
        argv += ["--expected-ids", args.expected_ids or ""]
    elif kind == "consolidation":
        if args.degrade:
            argv.append("--degrade")
        elif args.source_ids:
            argv += ["--source-ids", args.source_ids]
    return argv


def cmd_validate(args: argparse.Namespace) -> int:
    kind = args.kind
    if kind == "response" and not args.expected_ids:
        raise Abort("validate response needs --expected-ids")

    plan = None
    if args.session_id or args.plan_file:
        plan = load_plan(args.session_id, args.plan_file)
    check_files = args.check_files or (plan is not None and plan["mode"] == "code")
    deep = args.deep or (plan is not None and bool(plan["deep"]))

    source = Path(args.file)
    if not source.is_file():
        raise Abort(f"agent output file not found: {source}")
    cleaned = strip_fences(source.read_text(encoding="utf-8"))

    tmp_dir = _tmp_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{kind}_{_safe_name(args.session_id or 'nosid')}_r{args.round}"
    if args.lens:
        stem += f"_{args.lens}"
    staged = tmp_dir / f"{stem}.raw.json"
    staged.write_text(cleaned + "\n", encoding="utf-8")

    argv = _validator_argv(kind, staged, args, check_files, deep)
    result = subprocess.run(argv, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False,
                            cwd=str(plan["repo_root"]) if plan else None)

    if result.returncode == 0:
        out_path = Path(args.out) if args.out else tmp_dir / f"{stem}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result.stdout, encoding="utf-8")
        validated = json.loads(result.stdout)
        print(json.dumps({
            "status": "ok",
            "kind": kind,
            "lens": args.lens,
            "attempt": args.attempt,
            "degraded": bool(args.degrade),
            "out_file": str(out_path),
            "counts": tally(kind, validated),
            "result": validated,
        }, indent=2))
        return 0

    errors = (result.stderr or result.stdout).strip()
    if args.attempt < MAX_VALIDATION_ATTEMPTS:
        action = "retry"
    else:
        action = _fallback_action(kind, args.lens)
    decision = {
        "status": action,
        "kind": kind,
        "lens": args.lens,
        "attempt": args.attempt,
        "max_attempts": MAX_VALIDATION_ATTEMPTS,
        "errors": errors,
        "feedback": RETRY_FEEDBACK.format(errors=errors) if action == "retry" else None,
        "instruction": NEXT_ACTIONS[action].format(next=args.attempt + 1,
                                                   lens=args.lens or "?"),
    }
    print(json.dumps(decision, indent=2))
    return 3


# --------------------------------------------------------------------------- #
# `budget` — spend query, summary suffix, abort threshold
# --------------------------------------------------------------------------- #

def cmd_budget(args: argparse.Namespace) -> int:
    """Report spend for this run and decide whether it must abort.

    Known precision loss: round-2+ Defender continuations go through
    SendMessage, which produces no Agent tool entry, so their tokens carry no
    ``request_id`` and ``query.total_tokens_for_request`` cannot see them at
    all. The old skill text claimed a "parent-session fallback path" caught
    them — there is none (review §4.5). Reported spend is therefore a floor.
    """
    plan = None
    if args.session_id or args.plan_file:
        plan = load_plan(args.session_id, args.plan_file)
    budget = args.budget if args.budget is not None else (
        plan["budget_tokens"] if plan else DEFAULT_BUDGET_TOKENS)
    request_id = args.request_id or (plan["request_id"] if plan else None)
    cwd = args.cwd or (plan["session_cwd"] if plan else None)

    spent: int | None = None
    error: str | None = None

    if args.spent is not None:
        spent = args.spent
    elif request_id:
        script = Path(args.query_script) if args.query_script else QUERY_REQUEST
        argv = [sys.executable, str(script), "--request-id", request_id]
        if cwd:
            argv += ["--cwd", cwd]
        result = subprocess.run(argv, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False)
        raw = (result.stdout or "").strip()
        if result.returncode == 0 and INTEGER_RE.match(raw):
            spent = int(raw)
        else:
            error = ((result.stderr or raw).strip() or "no output")[:80]
    else:
        error = "no --spent and no request_id available"

    if spent is None:
        suffix = f"| spent: unknown ({error})"
        pct = None
        exceeded = False
    else:
        pct = round(100 * spent / budget) if budget > 0 else None
        suffix = (f"| spent {spent} of {budget} ({pct}%)" if pct is not None
                  else f"| spent {spent} of {budget} (n/a)")
        # The empty-findings exit means the run completed cleanly: never stamp
        # BUDGET EXCEEDED on that path even if spend went over.
        exceeded = bool(spent > budget and not args.empty_findings)

    payload = {
        "spent": spent,
        "budget": budget,
        "pct": pct,
        "error": error,
        "exceeded": exceeded,
        "suffix": suffix,
        "abort_message": (
            f"BUDGET EXCEEDED: spent {spent} > budget {budget}. Aborting after round "
            f"{args.round} with partial results." if exceeded else None),
        "instruction": (
            "Print abort_message, set budget_exceeded for the Step 4 summary, skip the "
            "remaining rounds and jump to Step 3. Do NOT remove the worktree."
            if exceeded else
            "Append `suffix` to the round summary line and continue. A null `spent` is "
            "not an abort."),
    }
    print(json.dumps(payload, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# `file-todos` / `save-summary` — scribe writes through the launcher
# --------------------------------------------------------------------------- #

def _scribe_add(launcher: Path, note_type: str, content: str, session_id: str,
                auto: bool, tmp_dir: Path, tag: str) -> tuple[bool, str]:
    """Write ``content`` to a temp file and add it as a scribe note.

    ``--content-file`` (not ``--content``) keeps long bodies off argv, which
    Windows caps at 32,767 characters.
    """
    payload = tmp_dir / f"note_{tag}.md"
    payload.write_text(content, encoding="utf-8")
    argv = [sys.executable, str(launcher), "scribe/notes.py", "add",
            "--type", note_type, "--content-file", str(payload),
            "--session-id", session_id]
    if auto:
        argv.append("--auto")
    result = subprocess.run(argv, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[:200]
    return True, result.stdout.strip()


def _deferred_todos(response: dict, findings, round_no: int) -> list[str]:
    described = {}
    for finding in findings or []:
        if isinstance(finding, dict) and isinstance(finding.get("id"), str):
            described[finding["id"]] = finding.get("description", "")

    out: list[str] = []
    for item in response.get("responses", []):
        if item.get("action") != "deferred":
            continue
        ref = item.get("finding_ref", "?")
        desc = described.get(ref, "")
        reason = item.get("description", "")
        out.append(f"Deferred {ref}: {desc} — Reason: {reason} "
                   f"(from /harden round {round_no})")
    return out


def cmd_file_todos(args: argparse.Namespace) -> int:
    plan = None
    if args.session_id or args.plan_file:
        plan = load_plan(args.session_id, args.plan_file)
    session_id = args.session_id or (plan["session_id"] if plan else None)
    if not session_id:
        raise Abort("file-todos needs --session-id")

    response = _read_json_file(Path(args.response), "defender response")
    findings = _load_optional(args.findings, "round findings")

    contents = [f"{t} (from /harden round {args.round})"
                for t in response.get("todos", []) if isinstance(t, str) and t.strip()]
    contents += _deferred_todos(response, findings, args.round)

    if args.dry_run:
        print(json.dumps({"filed": 0, "dry_run": True, "todos": contents}, indent=2))
        return 0

    repo = Path(args.repo).resolve() if args.repo else (
        Path(plan["repo_root"]) if plan else _repo_root(None))
    launcher = Path(args.launcher) if args.launcher else _default_launcher(repo)
    if not launcher.is_file():
        raise Abort(f"apiary launcher not found at {launcher} — re-run `apiary install`.")

    tmp_dir = _tmp_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    filed = 0
    for index, content in enumerate(contents):
        ok, message = _scribe_add(launcher, "todo", content, session_id, True,
                                  tmp_dir, f"{_safe_name(session_id)}_r{args.round}_{index}")
        if ok:
            filed += 1
        else:
            failed.append(f"{content[:60]}…: {message}")

    print(json.dumps({"filed": filed, "failed": failed, "todos": contents}, indent=2))
    return 1 if failed else 0


def cmd_save_summary(args: argparse.Namespace) -> int:
    plan = None
    if args.session_id or args.plan_file:
        plan = load_plan(args.session_id, args.plan_file)
    session_id = args.session_id or (plan["session_id"] if plan else None)
    if not session_id:
        raise Abort("save-summary needs --session-id")

    source = Path(args.content_file)
    if not source.is_file():
        raise Abort(f"summary file not found: {source}")
    content = source.read_text(encoding="utf-8")

    if args.dry_run:
        print(json.dumps({"saved": False, "dry_run": True, "chars": len(content)}, indent=2))
        return 0

    repo = Path(args.repo).resolve() if args.repo else (
        Path(plan["repo_root"]) if plan else _repo_root(None))
    launcher = Path(args.launcher) if args.launcher else _default_launcher(repo)
    if not launcher.is_file():
        raise Abort(f"apiary launcher not found at {launcher} — re-run `apiary install`.")

    tmp_dir = _tmp_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ok, message = _scribe_add(launcher, args.type, content, session_id, False,
                              tmp_dir, f"summary_{_safe_name(session_id)}")
    print(json.dumps({"saved": ok, "detail": message}, indent=2))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Harden orchestrator: plan, prompts, worktree, validation policy, "
                    "budget and TODO filing for /harden")
    sub = parser.add_subparsers(dest="command")

    p_plan = sub.add_parser("plan", help="Resolve targets, pick the path, size-check "
                                         "and estimate cost; writes the run plan")
    p_plan.add_argument("--session-id", required=True)
    p_plan.add_argument("--targets", nargs="*", default=[],
                        help="Code files and/or directories to harden")
    p_plan.add_argument("--plan-note", help="Scribe note id — plan mode instead of code mode")
    p_plan.add_argument("--lenses", help="Comma-separated lens subset (code mode)")
    p_plan.add_argument("--focus", choices=VALID_FOCUS,
                        help="Legacy single-attacker focus; providing it without "
                             "--lenses selects the legacy path")
    p_plan.add_argument("--deep", action="store_true",
                        help="Require Given/When/Then attack scenarios")
    p_plan.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    p_plan.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    p_plan.add_argument("--max-target-kb", type=int, default=DEFAULT_MAX_TARGET_KB)
    p_plan.add_argument("--model-attacker", default=DEFAULT_MODEL)
    p_plan.add_argument("--model-consolidator", default=DEFAULT_MODEL)
    p_plan.add_argument("--model-defender", default=DEFAULT_MODEL)
    p_plan.add_argument("--budget-tokens", type=int, default=DEFAULT_BUDGET_TOKENS)
    p_plan.add_argument("--request-id", help="Override the generated request id (tests)")
    p_plan.add_argument("--cwd", help="Session working directory (default: cwd)")
    p_plan.add_argument("--repo", help="Repo root override (default: git rev-parse)")
    p_plan.add_argument("--launcher", help="Path to .claude/apiary/launch.py (plan mode)")
    p_plan.add_argument("--out", help="Write the plan JSON here instead of harden/tmp/")
    p_plan.add_argument("--json", action="store_true",
                        help="Print the plan JSON instead of the human summary")
    p_plan.set_defaults(func=cmd_plan)

    p_prompt = sub.add_parser("prompt", help="Print a ready-to-spawn agent prompt")
    p_prompt.add_argument("role", choices=("attacker", "consolidator", "defender",
                                           "defender-continue"))
    p_prompt.add_argument("--session-id")
    p_prompt.add_argument("--plan-file")
    p_prompt.add_argument("--round", type=int, default=1)
    p_prompt.add_argument("--lens", action="append",
                          help="Limit to these lenses (repeatable); default: all resolved")
    p_prompt.add_argument("--findings", help="Validated findings JSON for this round")
    p_prompt.add_argument("--prev-findings", help="Previous round's validated findings")
    p_prompt.add_argument("--prev-response", help="Previous round's validated Defender JSON")
    p_prompt.add_argument("--rejections", help="Previous round's consolidation output")
    p_prompt.set_defaults(func=cmd_prompt)

    p_wt = sub.add_parser("worktree", help="Readiness check, create, remove or diff "
                                           "the run's worktree")
    p_wt.add_argument("action", choices=("check", "create", "remove", "diff"))
    p_wt.add_argument("--session-id")
    p_wt.add_argument("--plan-file")
    p_wt.add_argument("--repo", help="Repo root override")
    p_wt.add_argument("--delete-branch", action="store_true",
                      help="remove: also delete the harden branch")
    p_wt.set_defaults(func=cmd_worktree)

    p_round = sub.add_parser("round", help="Round counter and Defender agent id")
    p_round.add_argument("action", choices=("start", "tick", "status", "reset", "defender"))
    p_round.add_argument("--session-id", required=True)
    p_round.add_argument("--set", dest="set_id", help="defender: store this agent id")
    p_round.add_argument("--get", action="store_true", help="defender: print the agent id")
    p_round.set_defaults(func=cmd_round)

    p_val = sub.add_parser("validate", help="Validate agent output and decide "
                                            "retry / drop / ask / degrade")
    p_val.add_argument("kind", choices=("findings", "response", "consolidation"))
    p_val.add_argument("--file", required=True, help="Raw agent output (fences ok)")
    p_val.add_argument("--session-id")
    p_val.add_argument("--plan-file")
    p_val.add_argument("--round", type=int, default=1)
    p_val.add_argument("--attempt", type=int, default=1,
                       help="1 = first try, 2 = the one retry")
    p_val.add_argument("--lens", help="findings: per-lens mode")
    p_val.add_argument("--expected-ids", help="response: comma-separated finding ids")
    p_val.add_argument("--source-ids", help="consolidation: dispatched ATK ids")
    p_val.add_argument("--degrade", action="store_true",
                       help="consolidation: deterministic dedup fallback")
    p_val.add_argument("--check-files", action="store_true",
                       help="Force file-existence checks (implied in code mode)")
    p_val.add_argument("--deep", action="store_true",
                       help="Force deep-mode checks (implied by the plan)")
    p_val.add_argument("--out", help="Write the validated JSON here")
    p_val.set_defaults(func=cmd_validate)

    p_budget = sub.add_parser("budget", help="Spend query, summary suffix and abort check")
    p_budget.add_argument("action", choices=("check",))
    p_budget.add_argument("--session-id")
    p_budget.add_argument("--plan-file")
    p_budget.add_argument("--spent", type=int, help="Known spend; skips the log query")
    p_budget.add_argument("--budget", type=int, help="Override the plan's budget")
    p_budget.add_argument("--request-id")
    p_budget.add_argument("--cwd", help="Session cwd, so the right project log is read")
    p_budget.add_argument("--round", type=int, default=1)
    p_budget.add_argument("--empty-findings", action="store_true",
                          help="Clean empty-findings exit: never mark BUDGET EXCEEDED")
    p_budget.add_argument("--query-script",
                          help="Override budgeter/query_request.py (tests)")
    p_budget.set_defaults(func=cmd_budget)

    p_todos = sub.add_parser("file-todos", help="File Defender todos and deferred "
                                                "findings as scribe todos")
    p_todos.add_argument("--response", required=True, help="Validated Defender JSON")
    p_todos.add_argument("--findings", help="This round's validated findings (for descriptions)")
    p_todos.add_argument("--session-id")
    p_todos.add_argument("--plan-file")
    p_todos.add_argument("--round", type=int, default=1)
    p_todos.add_argument("--repo", help="Repo root override")
    p_todos.add_argument("--launcher", help="Path to .claude/apiary/launch.py")
    p_todos.add_argument("--dry-run", action="store_true",
                         help="Print what would be filed, write nothing")
    p_todos.set_defaults(func=cmd_file_todos)

    p_save = sub.add_parser("save-summary", help="Save the run summary as a scribe note")
    p_save.add_argument("--content-file", required=True, dest="content_file")
    p_save.add_argument("--session-id")
    p_save.add_argument("--plan-file")
    p_save.add_argument("--type", default="context", help="Scribe note type (default context)")
    p_save.add_argument("--repo", help="Repo root override")
    p_save.add_argument("--launcher", help="Path to .claude/apiary/launch.py")
    p_save.add_argument("--dry-run", action="store_true")
    p_save.set_defaults(func=cmd_save_summary)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except Abort as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
