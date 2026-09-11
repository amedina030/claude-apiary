#!/usr/bin/env python3
"""Caller-side command line for cross-repo Claude-to-Claude calls.

Usage (via the per-repo launcher)::

    telephone/cli.py call <repo> <message> [--act] [--wait] [--model M]
    telephone/cli.py reply <call-id> <message> [--wait] [--model M]
    telephone/cli.py status [<call-id>]
    telephone/cli.py show <call-id>
    telephone/cli.py list [--limit N] [--status S] [--callee NAME]
    telephone/cli.py hangup <call-id> [--kill]

``call`` resolves the callee in the registry, checks the mode and the caps,
spawns a headless ``claude -p`` run with the callee's checkout as the working
directory, blocks until the reply or the wall-clock limit, writes the record,
and prints the reply. The ``/telephone`` skill runs it through the Bash tool's
``run_in_background`` so the caller session is re-invoked when the reply lands.

Exit codes:
    0  the call completed (answered, timed out, or the callee reported an error)
    1  refused before any callee run started, or a bad argument
    2  the ``claude`` binary could not be launched (mirrors ``run_claude``'s -2)

Act mode is off by default and is never available to a call Claude placed on
its own: it needs a grant file that only the ``UserPromptSubmit`` hook writes,
and only when the user typed ``/telephone <repo> act ...`` themselves.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.session import session_tmp_dir  # noqa: E402
from core.utils import state  # noqa: E402
from core.utils.timeutil import now_iso, parse_iso  # noqa: E402
from runner.claude_subprocess import (  # noqa: E402
    describe_failure,
    run_claude,
    scrub_claude_code_env,
)
from telephone import protocol, store  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_NO_BINARY = 2

#: Set in every callee's environment. Its presence is how a callee's own
#: ``call`` refuses a nested call.
TELEPHONE_ENV_VAR = "APIARY_TELEPHONE_CALL"

#: Session flag files. The grant is written by ``telephone/hooks/user_prompt.py``
#: and consumed here; the counter is owned by this CLI.
GRANT_SUFFIX = "telephone_grant"
COUNTER_SUFFIX = "telephone_auto_calls"

#: Caller-scoped variables that would misroute the callee's hooks to the
#: caller's state directory if they were inherited.
_CALLER_SCOPED_ENV = (
    "CLAUDE_PROJECT_DIR",
    "APIARY_TARGET_REPO",
    "APIARY_TARGET_STATE_DIR",
    "APIARY_MAIN_REPO",
    "APIARY_RUNNER_SUBPROCESS",
)

#: How much of a reply `show` prints per exchange before eliding. The caller's
#: context is the scarce resource this whole tool exists to protect.
SHOW_REPLY_CHARS = 2000

BRANCH_PREFIX = "telephone/"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _fail(message: str, code: int = EXIT_REFUSED) -> int:
    print(f"telephone: {message}", file=sys.stderr)
    return code


def _git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run one git command against *repo*. Never raises on a non-zero exit."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            args=list(args), returncode=1, stdout="", stderr=str(exc)
        )


def current_branch(repo: Path) -> str:
    out = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out.stdout.strip() if out.returncode == 0 else ""


def default_branch(repo: Path) -> str:
    """The callee's default branch: origin's HEAD, else master, else main."""
    out = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip().split("/", 1)[-1]
    for candidate in ("master", "main"):
        if _git(repo, "rev-parse", "--verify", "--quiet", candidate).returncode == 0:
            return candidate
    return ""


def dirty_paths(repo: Path) -> list[str]:
    out = _git(repo, "status", "--porcelain")
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def branch_exists(repo: Path, name: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0


def remote_refs(repo: Path) -> dict[str, str]:
    """Every remote-tracking ref and the commit it points at.

    Compared before and after an act call: an unattended session in another
    repo must never publish, and a changed tracking ref is the evidence that
    one did.
    """
    out = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes")
    if out.returncode != 0:
        return {}
    refs: dict[str, str] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            refs[parts[0]] = parts[1]
    return refs


# --------------------------------------------------------------------------- #
# Registry resolution
# --------------------------------------------------------------------------- #


def registered_names(apiary: Path) -> list[str]:
    registry = state._load_registry(apiary)
    names = [e.get("name", "") for e in registry.values() if isinstance(e, dict)]
    return sorted(n for n in names if n)


def resolve_callee(apiary: Path, target: str) -> tuple[str, Path]:
    """Resolve a registry name or a repo path to ``(name, real_path)``.

    Raises ``LookupError`` with a message meant for the caller's session: an
    unknown name lists the registered names, an ambiguous one lists the
    candidates, and a registered entry whose checkout is gone names the path.
    """
    registry = state._load_registry(apiary)
    raw = (target or "").strip()
    if not raw:
        raise LookupError("no repo named")

    candidate = Path(raw)
    if candidate.is_absolute() or any(sep in raw for sep in ("/", "\\")):
        resolved = candidate.resolve()
        for entry in registry.values():
            if isinstance(entry, dict) and Path(str(entry.get("real_path", ""))) == resolved:
                return str(entry.get("name") or resolved.name), resolved
        raise LookupError(
            f"{resolved} is not a registered repo. Registered: {', '.join(registered_names(apiary))}"
        )

    matches = [e for e in registry.values() if isinstance(e, dict) and e.get("name") == raw]
    if not matches:
        raise LookupError(
            f"no registered repo named {raw!r}. Registered: {', '.join(registered_names(apiary))}"
        )
    if len(matches) > 1:
        paths = ", ".join(str(e.get("real_path")) for e in matches)
        raise LookupError(f"repo name {raw!r} is ambiguous ({paths}). Pass the repo path instead")
    path = Path(str(matches[0].get("real_path", "")))
    if not path.is_dir():
        raise LookupError(f"registered repo {raw!r} has no checkout at {path}")
    return raw, path.resolve()


def caller_identity(apiary: Path) -> tuple[str, Path | None]:
    """``(name, repo)`` for the repo this CLI is running in."""
    from core.flags import _per_repo_root

    repo = _per_repo_root()
    if repo is None:
        return "unknown", None
    repo = Path(repo).resolve()
    registry = state._load_registry(apiary)
    for entry in registry.values():
        if isinstance(entry, dict) and Path(str(entry.get("real_path", ""))) == repo:
            return str(entry.get("name") or repo.name), repo
    return repo.name, repo


# --------------------------------------------------------------------------- #
# Session grants and the autonomous cap
# --------------------------------------------------------------------------- #


#: Claude Code exports the running session's id to every Bash tool process.
#: It is the one deterministic way for a CLI outside the hook chain to know
#: which session it serves. ``core.session.load_identity()`` was tried first
#: and picked the most recently started session on the repo, which turned a
#: user-typed call into an autonomous one the moment a second session had
#: touched the checkout (seen live 2026-09-11).
SESSION_ENV_VAR = "CLAUDE_CODE_SESSION_ID"

#: Counter key when no session id is known at all, so the autonomous cap still
#: applies to a CLI run from outside any session.
NO_SESSION_KEY = "nosession"


def session_prefix(explicit: str | None = None) -> str:
    """The caller session's 8-char id prefix, or ``""`` when none is known.

    *explicit* (``--session-id``) wins, then :data:`SESSION_ENV_VAR`. Nothing
    is guessed from identity files. The grant file the hook wrote is named
    with the full uuid, which is why lookups below glob on the prefix.
    """
    raw = (explicit or os.environ.get(SESSION_ENV_VAR) or "").strip()
    return raw[:8].lower()


def _flag_dir() -> Path:
    return session_tmp_dir(warn=False)


def _flag_matches(prefix: str, suffix: str) -> list[Path]:
    """Flag files for *prefix* ending in *suffix*, newest first."""
    if not prefix:
        return []
    root = _flag_dir()
    if not root.is_dir():
        return []
    try:
        found = [p for p in root.glob(f"{prefix}*_{suffix}") if p.is_file()]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def read_grant(prefix: str) -> dict | None:
    """The newest grant this session holds, or ``None``."""
    for path in _flag_matches(prefix, GRANT_SUFFIX):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return {**data, "_path": str(path)}
    return None


def grant_age_seconds(grant: dict) -> float | None:
    """Seconds since the hook wrote *grant*, or ``None`` when it has no stamp."""
    stamp = parse_iso(grant.get("granted_at"))
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def usable_grant(
    prefix: str, callee_path: Path, apiary: Path, config: dict
) -> tuple[dict | None, str]:
    """The grant this call may spend, and the reason when there is none.

    A grant is spent only on the repo the user named and only while it is
    fresh. Two consequences: a grant typed for one repo never unlocks act mode
    on another, and a grant the model never used cannot be picked up later in
    the session as if the user had just asked. An expired grant is deleted. A
    grant for another repo is left where it is, because the user may still
    want that call placed.
    """
    grant = read_grant(prefix)
    if grant is None:
        return None, "no grant for this session"
    ttl = int(config.get("grant_ttl_seconds") or 0)
    age = grant_age_seconds(grant)
    if ttl and (age is None or age > ttl):
        _discard_grant(grant)
        shown = "unknown" if age is None else f"{int(age)}s"
        return None, f"the grant had expired (age {shown}, limit {ttl}s)"
    named = str(grant.get("repo") or "").strip()
    if not named:
        return None, "the grant names no repo"
    try:
        named_name, named_path = resolve_callee(apiary, named)
    except LookupError:
        return None, f"the grant names {named!r}, which is not a registered repo"
    if named_path != callee_path:
        return None, f"the grant is for {named_name}, not this callee"
    return grant, ""


def _discard_grant(grant: dict) -> None:
    try:
        Path(grant["_path"]).unlink()
    except (KeyError, OSError):
        pass


def consume_grant(grant: dict | None) -> None:
    """Spend *grant*. One typed command buys exactly one call.

    Called only after every refusal check has passed, so a call that never
    started (dirty tree, self call, unknown repo) leaves the user's grant in
    place for the retry.
    """
    if grant is not None:
        _discard_grant(grant)


def _counter_path(prefix: str) -> Path:
    key = prefix or NO_SESSION_KEY
    existing = _flag_matches(key, COUNTER_SUFFIX)
    if existing:
        return existing[0]
    return _flag_dir() / f"{key}_{COUNTER_SUFFIX}"


def auto_call_count(prefix: str) -> int:
    try:
        return int(_counter_path(prefix).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def bump_auto_calls(prefix: str) -> int:
    """Increment the session's autonomous-call count and return the new value.

    Same shape as ``core/hooks/compass_rules.py::bump_counter``: the count
    lives on disk because a model cannot be asked to keep its own tally. A
    run with no session id at all shares the :data:`NO_SESSION_KEY` counter.
    """
    path = _counter_path(prefix)
    count = auto_call_count(prefix) + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(count), encoding="utf-8")
    except OSError:
        pass
    return count


# --------------------------------------------------------------------------- #
# The callee run
# --------------------------------------------------------------------------- #


def child_env(call_id: str) -> dict[str, str]:
    """The environment for a callee run.

    Three edits to the parent's environment, each load-bearing:

    * ``CLAUDECODE`` and every ``CLAUDE_CODE_*`` variable go, so the child is
      not treated as a sub-invocation of this session;
    * the caller-scoped apiary pointers go, so the callee's hooks resolve the
      callee's own state directory rather than the caller's;
    * ``APIARY_TELEPHONE_CALL`` is added, which is what makes a nested call
      refusable.

    Everything else is inherited, because the callee is meant to run its own
    hook chain and answer with its own context (verified live, L-2026-191).
    """
    env = scrub_claude_code_env(dict(os.environ))
    for name in _CALLER_SCOPED_ENV:
        env.pop(name, None)
    env[TELEPHONE_ENV_VAR] = call_id
    return env


def spawn_callee(
    *,
    prompt: str,
    callee_path: Path,
    call_id: str,
    mode_cfg: dict,
    model: str,
    timeout: int,
    resume: str | None = None,
) -> tuple[int, str, str]:
    """Run the callee and return ``(returncode, stdout, stderr)``.

    ``rules=False`` because the callee's own startup hook delivers its compass
    table; prepending the caller's would describe the wrong repo.
    """
    return run_claude(
        prompt,
        timeout=timeout,
        model=model or None,
        max_turns=mode_cfg.get("max_turns") or None,
        allowed_tools=tuple(mode_cfg.get("allowed_tools") or ()),
        disallowed_tools=tuple(mode_cfg.get("disallowed_tools") or ()),
        permission_mode=(mode_cfg.get("permission_mode") or None),
        rules=False,
        resume=resume,
        cwd=callee_path,
        env=child_env(call_id),
        capture_partial_on_timeout=True,
        output_format="stream-json",
    )


def _json_lines(stdout: str) -> list[dict]:
    """Every JSON object on its own line in *stdout*, in order."""
    rows: list[dict] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def read_envelope(stdout: str) -> dict:
    """The result envelope in *stdout*, from either claude output format.

    ``json`` prints one object. ``stream-json`` (what the callee runs with)
    prints one event per line, and the last ``type: result`` line is the
    envelope. A killed run has no envelope and returns ``{}``.
    """
    try:
        data = json.loads(stdout or "")
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        return data
    for row in reversed(_json_lines(stdout)):
        if row.get("type") == "result":
            return row
    return {}


def partial_text(stdout: str) -> str:
    """Assistant text a ``stream-json`` run wrote before it stopped.

    This is what makes a timed-out call leave something on the record: each
    assistant turn arrives as its own event while the run is still going, so
    the text is on disk even when the envelope never came.
    """
    parts: list[str] = []
    for row in _json_lines(stdout):
        if row.get("type") != "assistant":
            continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
    return "\n".join(p for p in parts if p).strip()


# --------------------------------------------------------------------------- #
# Scribe notes on both sides
# --------------------------------------------------------------------------- #


def launcher_for(repo: Path) -> Path | None:
    path = Path(repo) / ".claude" / "apiary" / "launch.py"
    return path if path.is_file() else None


def note_text(
    *,
    call_id: str,
    caller: str,
    callee: str,
    mode: str,
    status: str,
    initiated_by: str,
    record: Path,
    message: str,
    issue: str = "",
    branch: str = "",
) -> str:
    """The context note both repos get. Kept short and plain on purpose."""
    lines = [
        f"Telephone call {call_id}: {caller} called {callee} in {mode} mode.",
        "",
        f"Status: {status}. Placed by: {initiated_by}.",
        f"Record: {record}",
    ]
    if branch:
        lines.append(f"Work branch in {callee}: {branch}")
    if issue:
        lines.append(f"Issue: {issue}")
    first = " ".join((message or "").split())[:200]
    if first:
        lines.append(f"Asked: {first}")
    return "\n".join(lines) + "\n"


def write_note(repo: Path, text: str) -> str:
    """Add a ``context`` note to *repo*'s scribe store through its launcher.

    Retried with ``--force`` when the first attempt fails, so a prose gate that
    refuses the wording can never lose the pointer to the record. Returns a
    short outcome string for the caller's status block.
    """
    launcher = launcher_for(repo)
    if launcher is None:
        return f"{repo.name}: no launcher, note skipped"
    # Without this the callee-side note inherits the caller's
    # ``CLAUDE_PROJECT_DIR``, and every per-repo lookup scribe makes on the way
    # in (the store, the prose-gate flag) resolves the *caller's* repo instead.
    # The launcher re-exports the right values from the target's own pins.
    env = {k: v for k, v in os.environ.items() if k not in _CALLER_SCOPED_ENV}
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8", prefix="telephone-note-"
    )
    try:
        handle.write(text)
        handle.close()
        base = [
            sys.executable,
            str(launcher),
            "scribe/notes.py",
            "add",
            "--type",
            "context",
            "--content-file",
            handle.name,
            "--tags",
            "telephone",
        ]
        for attempt in (base, [*base, "--force"]):
            try:
                result = subprocess.run(
                    attempt,
                    cwd=str(repo),
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return f"{repo.name}: note failed ({exc.__class__.__name__})"
            if result.returncode == 0:
                forced = " (forced)" if attempt is not base else ""
                return f"{repo.name}: {(result.stdout or '').strip() or 'note added'}{forced}"
        return f"{repo.name}: note refused ({(result.stderr or '').strip()[:120]})"
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# call
# --------------------------------------------------------------------------- #


def _nested_guard() -> str | None:
    active = os.environ.get(TELEPHONE_ENV_VAR, "").strip()
    if active:
        return (
            f"refusing a nested call: this session is already inside telephone call {active}. "
            "Say what you need under the questions heading and let the caller decide."
        )
    return None


def _act_preflight(callee_path: Path, callee: str, apiary: Path) -> str | None:
    """The reason an act call must not start, or ``None``."""
    dirty = dirty_paths(callee_path)
    if dirty:
        listed = ", ".join(line.strip() for line in dirty[:5])
        return f"act refused: {callee}'s tree is dirty ({len(dirty)} path(s): {listed})"
    branch = current_branch(callee_path)
    default = default_branch(callee_path)
    if not branch:
        return f"act refused: cannot read {callee}'s current branch"
    if default and branch != default:
        return f"act refused: {callee} is on branch {branch}, not its default branch {default}"
    open_call = store.open_act_call(callee, apiary)
    if open_call:
        return f"act refused: act call {open_call} is already open against {callee}"
    return None


def cmd_call(args) -> int:
    nested = _nested_guard()
    if nested:
        return _fail(nested)

    try:
        apiary = state.resolve_apiary_repo(Path(args.apiary_repo) if args.apiary_repo else None)
    except RuntimeError as exc:
        return _fail(str(exc))

    try:
        callee, callee_path = resolve_callee(apiary, args.repo)
    except LookupError as exc:
        return _fail(str(exc))

    caller, caller_path = caller_identity(apiary)
    if caller_path is not None and callee_path == caller_path:
        return _fail(f"refusing a self call: {callee} is the repo this session is already in")

    prefix = session_prefix(args.session_id)
    config = store.load_config()
    grant, why_not = usable_grant(prefix, callee_path, apiary, config)
    initiated_by = "user" if grant else "model"

    if args.act and not (grant and grant.get("act")):
        detail = why_not if grant is None else "the grant the user typed was for answer mode"
        return _fail(
            "act needs the user to type `/telephone "
            f"{callee} act <message>` themselves ({detail}), so the call was not placed."
        )
    mode = store.MODE_ACT if args.act else store.MODE_ANSWER

    if grant is None:
        cap = int(config.get("max_autonomous_calls_per_session") or 0)
        if cap and auto_call_count(prefix) >= cap:
            return _fail(
                f"autonomous call cap reached ({cap} this session). Ask the user to place "
                f"this one with `/telephone {callee} <message>`."
            )

    if mode == store.MODE_ACT:
        refusal = _act_preflight(callee_path, callee, apiary)
        if refusal:
            return _fail(refusal)

    # Past every refusal. The grant is spent and the counter moves only now, so
    # a refused call costs neither the user's grant nor one of the model's slots.
    if grant is None:
        bump_auto_calls(prefix)
    else:
        consume_grant(grant)

    call_id = store.allocate_id(apiary)
    branch = f"{BRANCH_PREFIX}{call_id}" if mode == store.MODE_ACT else ""
    mode_cfg = store.mode_config(config, mode)
    timeout = int(args.timeout or mode_cfg.get("timeout_seconds") or 600)
    model = args.model or str(config.get("model") or "")
    path = store.record_path(call_id, apiary)

    meta = {
        "id": call_id,
        "mode": mode,
        "status": store.STATUS_OPEN,
        "initiated_by": initiated_by,
        "caller": caller,
        "caller_path": str(caller_path or ""),
        "callee": callee,
        "callee_path": str(callee_path),
        "callee_session_id": "",
        "model": model,
        "branch": branch,
        "started_at": now_iso(),
        "ended_at": "",
        "pid": str(os.getpid()),
        "exchanges": "0",
        "issue": "",
    }
    header = f"# Call {call_id}: {caller} to {callee}\n"
    store.write_record(path, meta, header)

    before_refs: dict[str, str] = {}
    start_branch = ""
    if mode == store.MODE_ACT:
        # The CLI owns the branch, not the callee's good behaviour: it is
        # created here, before the run, and put back by _settle_act after it.
        start_branch = current_branch(callee_path)
        before_refs = remote_refs(callee_path)
        problem = _enter_work_branch(callee_path, branch)
        if problem:
            meta.update({"status": store.STATUS_FAILED, "ended_at": now_iso(), "issue": problem})
            store.write_record(path, meta, header)
            return _fail(f"{problem} in {callee}, so the call was not placed")

    prompt = protocol.build_preamble(
        caller=caller,
        callee=callee,
        mode=mode,
        call_id=call_id,
        message=args.message,
        branch=branch or None,
        initiated_by=initiated_by,
    )
    started = time.monotonic()
    rc, stdout, stderr = spawn_callee(
        prompt=prompt,
        callee_path=callee_path,
        call_id=call_id,
        mode_cfg=mode_cfg,
        model=model,
        timeout=timeout,
    )
    elapsed = round(time.monotonic() - started, 1)

    if rc == -2:
        meta.update({"status": store.STATUS_FAILED, "ended_at": now_iso()})
        store.write_record(path, meta, f"# Call {call_id}: {caller} to {callee}\n")
        return _fail(stderr or "could not launch the claude binary", EXIT_NO_BINARY)

    notes: list[str] = []
    issue = ""
    if mode == store.MODE_ACT:
        issue, act_notes = _settle_act(callee_path, callee, branch, start_branch, before_refs)
        notes.extend(act_notes)

    return _finish_exchange(
        path=path,
        meta=meta,
        exchange_number=1,
        sent=args.message,
        rc=rc,
        stdout=stdout,
        stderr=stderr,
        elapsed=elapsed,
        timeout=timeout,
        issue=issue,
        extra_notes=notes,
        caller_path=caller_path,
        callee_path=callee_path,
    )


def _enter_work_branch(callee_path: Path, branch: str) -> str | None:
    """Check the callee out on *branch*, creating it when needed.

    Returns the problem as text, or ``None`` when the checkout is on the
    branch. The preflight has already established a clean tree on the default
    branch for a first call; a follow-up finds the branch already there.
    """
    if not branch:
        return None
    if branch_exists(callee_path, branch):
        switched = _git(callee_path, "checkout", branch)
    else:
        switched = _git(callee_path, "checkout", "-b", branch)
    if switched.returncode != 0:
        detail = " ".join((switched.stderr or switched.stdout or "").split())[:160]
        return f"could not check out the work branch {branch} ({detail})"
    return None


def _settle_act(
    callee_path: Path, callee: str, branch: str, start_branch: str, before_refs: dict
) -> tuple[str, list[str]]:
    """Put the callee's checkout back and report what the act call left behind.

    The CLI created the work branch, so it also owns the way back. A clean tree
    is switched to the branch the checkout started on. A dirty tree stays on
    the work branch and is flagged, because switching would carry the
    uncommitted edits along. Returns ``(issue, record notes)``, where *issue*
    is empty when nothing needs a human.
    """
    notes: list[str] = []
    issues: list[str] = []

    after_refs = remote_refs(callee_path)
    if before_refs != after_refs:
        moved = sorted(
            ref
            for ref in set(before_refs) | set(after_refs)
            if before_refs.get(ref) != after_refs.get(ref)
        )
        issues.append("push detected")
        notes.append(f"issue: push detected ({', '.join(moved[:5])})")

    has_branch = bool(branch) and branch_exists(callee_path, branch)
    if branch and not has_branch:
        issues.append("work branch missing")
        notes.append(f"branch missing: {branch} is gone from {callee}")

    ended_on = current_branch(callee_path)
    if branch and ended_on and ended_on != branch:
        notes.append(f"callee moved the checkout to {ended_on} during the call")

    if has_branch and start_branch:
        count = _git(callee_path, "rev-list", "--count", f"{start_branch}..{branch}")
        if count.returncode == 0:
            notes.append(f"commits on {branch}: {count.stdout.strip() or '0'}")

    dirty = dirty_paths(callee_path)
    if dirty:
        where = ended_on or branch
        issues.append(f"uncommitted changes left on {where}")
        notes.append(f"issue: uncommitted changes left on {where} ({len(dirty)} path(s))")
    elif start_branch and ended_on != start_branch:
        back = _git(callee_path, "checkout", start_branch)
        if back.returncode == 0:
            notes.append(f"checkout restored to {start_branch}")
        else:
            detail = " ".join((back.stderr or "").split())[:120]
            issues.append(f"checkout left on {ended_on}")
            notes.append(f"issue: could not restore {start_branch} ({detail})")

    return " and ".join(issues), notes


# --------------------------------------------------------------------------- #
# Finishing an exchange (shared by call and reply)
# --------------------------------------------------------------------------- #


def _finish_exchange(
    *,
    path: Path,
    meta: dict,
    exchange_number: int,
    sent: str,
    rc: int,
    stdout: str,
    stderr: str,
    elapsed: float,
    timeout: int,
    issue: str,
    extra_notes: list[str],
    caller_path: Path | None,
    callee_path: Path,
    resume_failed: bool = False,
) -> int:
    """Parse the envelope, append the exchange, write both notes, print the reply."""
    envelope = read_envelope(stdout)
    text = str(envelope.get("result") or "")
    callee_session = str(envelope.get("session_id") or "") or str(
        meta.get("callee_session_id") or ""
    )
    notes = list(extra_notes)

    if rc == -1:
        status = store.STATUS_TIMED_OUT
        # A killed run leaves no envelope. The stream-json events it did write
        # carry every assistant turn so far, and that text is the partial reply.
        partial = text or partial_text(stdout)
        if not partial and (stdout or "").strip() and not (stdout or "").lstrip().startswith("{"):
            partial = (stdout or "").strip()
        reply_text = partial or "(no text was flushed before the limit)"
        printed = (
            f"The line to {meta.get('callee')} timed out after {timeout}s and the run was killed."
        )
        if partial:
            printed += f"\nPartial text on the record:\n{partial[:1000]}"
        parsed = {"answer": printed, "questions": [], "changes": [], "structured": False}
    elif rc != 0 or envelope.get("is_error"):
        status = store.STATUS_FAILED
        envelope_json = json.dumps(envelope) if envelope else stdout
        reason = (stderr or "").strip() or describe_failure(envelope_json, rc)
        reply_text = text or (stdout or "").strip() or reason
        notes.append(f"failure: {' '.join(reason.split())[:300]}")
        parsed = {
            "answer": f"The call failed. {reason}" + (f"\n{text}" if text else ""),
            "questions": [],
            "changes": [],
            "structured": False,
        }
    else:
        status = store.STATUS_ANSWERED
        reply_text = text
        parsed = protocol.parse_reply(text)

    if resume_failed:
        notes.append("resume failed: a fresh callee run was started with the line quoted")
    if not parsed.get("structured") and status == store.STATUS_ANSWERED:
        notes.append("reply did not use the three headings; recorded verbatim")

    section = store.render_exchange(
        exchange_number,
        sent=sent,
        reply=reply_text,
        status=status,
        at=now_iso(),
        duration_s=elapsed,
        notes=notes,
    )
    updates = {
        "status": status,
        "ended_at": now_iso(),
        "callee_session_id": callee_session,
    }
    if issue:
        updates["issue"] = issue
    meta = store.append_exchange(path, updates, section)

    text_for_note = note_text(
        call_id=str(meta.get("id")),
        caller=str(meta.get("caller")),
        callee=str(meta.get("callee")),
        mode=str(meta.get("mode")),
        status=status,
        initiated_by=str(meta.get("initiated_by")),
        record=path,
        message=sent,
        issue=str(meta.get("issue") or ""),
        branch=str(meta.get("branch") or ""),
    )
    note_results = []
    for repo in (caller_path, callee_path):
        if repo is not None:
            note_results.append(write_note(Path(repo), text_for_note))

    print(
        protocol.format_reply(
            parsed,
            call_id=str(meta.get("id")),
            mode=str(meta.get("mode")),
            initiated_by=str(meta.get("initiated_by")),
            status=status,
        )
    )
    lines = [
        f"[telephone] callee={meta.get('callee')} exchange={meta.get('exchanges')} took={elapsed}s"
    ]
    if meta.get("branch"):
        lines.append(f"[telephone] branch={meta.get('branch')}")
    for note in notes:
        lines.append(f"[telephone] {note}")
    for result in note_results:
        lines.append(f"[telephone] note {result}")
    lines.append(f"[telephone] record={path}")
    if status == store.STATUS_ANSWERED and parsed.get("questions"):
        lines.append(
            "[telephone] the callee asked something back. Answer it with "
            f"`telephone/cli.py reply {meta.get('id')} <message>` or bring it to the user."
        )
    print("\n".join(lines))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# reply
# --------------------------------------------------------------------------- #


def cmd_reply(args) -> int:
    nested = _nested_guard()
    if nested:
        return _fail(nested)

    try:
        apiary = state.resolve_apiary_repo(Path(args.apiary_repo) if args.apiary_repo else None)
    except RuntimeError as exc:
        return _fail(str(exc))

    try:
        path, meta, body = store.load(args.call_id, apiary)
    except (ValueError, FileNotFoundError) as exc:
        return _fail(str(exc))

    mode = str(meta.get("mode") or store.MODE_ANSWER)
    callee = str(meta.get("callee") or "")
    callee_path = Path(str(meta.get("callee_path") or ""))
    if not callee_path.is_dir():
        return _fail(f"{callee} has no checkout at {callee_path}")

    prefix = session_prefix(args.session_id)
    config = store.load_config()
    grant, why_not = usable_grant(prefix, callee_path, apiary, config)

    if mode == store.MODE_ACT:
        if not (grant and grant.get("act")):
            detail = why_not if grant is None else "the grant the user typed was for answer mode"
            return _fail(
                "act follow-ups need the user to type `/telephone "
                f"{callee} act <message>` again ({detail}), so nothing was sent."
            )
        initiated_by = "user"
        dirty = dirty_paths(callee_path)
        if dirty:
            listed = ", ".join(line.strip() for line in dirty[:5])
            return _fail(
                f"act follow-up refused: {callee}'s tree is dirty "
                f"({len(dirty)} path(s): {listed}). Commit or discard that first."
            )
    else:
        initiated_by = "user" if grant else "model"
        if grant is None:
            cap = int(config.get("max_autonomous_exchanges_per_line") or 0)
            done = store.exchange_count(body)
            if cap and done >= cap:
                return _fail(
                    f"{args.call_id} already has {done} exchanges, which is the autonomous "
                    f"limit of {cap} on one line. Bring the thread to the user instead."
                )

    # Past every refusal, so the grant is spent only for a follow-up that runs.
    consume_grant(grant)

    mode_cfg = store.mode_config(config, mode)
    timeout = int(args.timeout or mode_cfg.get("timeout_seconds") or 600)
    model = args.model or str(meta.get("model") or config.get("model") or "")
    call_id = str(meta.get("id") or args.call_id)
    resume_id = str(meta.get("callee_session_id") or "").strip()
    branch = str(meta.get("branch") or "") or None

    before_refs: dict[str, str] = {}
    start_branch = ""
    if mode == store.MODE_ACT:
        start_branch = current_branch(callee_path)
        before_refs = remote_refs(callee_path)
        problem = _enter_work_branch(callee_path, branch or "")
        if problem:
            return _fail(f"{problem} in {callee}, so nothing was sent")

    prior = store.parse_exchanges(body) if not resume_id else None
    prompt = protocol.build_preamble(
        caller=str(meta.get("caller") or "unknown"),
        callee=callee,
        mode=mode,
        call_id=call_id,
        message=args.message,
        branch=branch,
        prior_exchanges=prior,
        initiated_by=initiated_by,
    )

    started = time.monotonic()
    rc, stdout, stderr = spawn_callee(
        prompt=prompt,
        callee_path=callee_path,
        call_id=call_id,
        mode_cfg=mode_cfg,
        model=model,
        timeout=timeout,
        resume=resume_id or None,
    )
    resume_failed = False
    if resume_id and rc not in (0, -1):
        # A stale or unreadable session id must not end the line. Start a fresh
        # callee run with the exchanges so far quoted in the preamble.
        resume_failed = True
        prompt = protocol.build_preamble(
            caller=str(meta.get("caller") or "unknown"),
            callee=callee,
            mode=mode,
            call_id=call_id,
            message=args.message,
            branch=branch,
            prior_exchanges=store.parse_exchanges(body),
            initiated_by=initiated_by,
        )
        started = time.monotonic()
        rc, stdout, stderr = spawn_callee(
            prompt=prompt,
            callee_path=callee_path,
            call_id=call_id,
            mode_cfg=mode_cfg,
            model=model,
            timeout=timeout,
        )
    elapsed = round(time.monotonic() - started, 1)

    if rc == -2:
        return _fail(stderr or "could not launch the claude binary", EXIT_NO_BINARY)

    issue = ""
    notes: list[str] = []
    if mode == store.MODE_ACT:
        issue, notes = _settle_act(callee_path, callee, branch or "", start_branch, before_refs)

    caller_path_raw = str(meta.get("caller_path") or "")
    caller_path = (
        Path(caller_path_raw) if caller_path_raw and Path(caller_path_raw).is_dir() else None
    )
    return _finish_exchange(
        path=path,
        meta=meta,
        exchange_number=store.exchange_count(body) + 1,
        sent=args.message,
        rc=rc,
        stdout=stdout,
        stderr=stderr,
        elapsed=elapsed,
        timeout=timeout,
        issue=issue,
        extra_notes=notes,
        caller_path=caller_path,
        callee_path=callee_path,
        resume_failed=resume_failed,
    )


# --------------------------------------------------------------------------- #
# status / show / list / hangup
# --------------------------------------------------------------------------- #


def _apiary_or_fail(args):
    return state.resolve_apiary_repo(Path(args.apiary_repo) if args.apiary_repo else None)


def _row(meta: dict) -> str:
    return (
        f"{meta.get('id', '?'):<12} {meta.get('status', '?'):<10} {meta.get('mode', '?'):<7} "
        f"{meta.get('caller', '?')} -> {meta.get('callee', '?')}  "
        f"exchanges={meta.get('exchanges', '0')} started={meta.get('started_at', '?')}"
    )


def cmd_status(args) -> int:
    try:
        apiary = _apiary_or_fail(args)
    except RuntimeError as exc:
        return _fail(str(exc))
    if args.call_id:
        try:
            path, meta, body = store.load(args.call_id, apiary)
        except (ValueError, FileNotFoundError) as exc:
            return _fail(str(exc))
        print(_row(meta))
        if meta.get("issue"):
            print(f"issue: {meta['issue']}")
        print(f"record: {path}")
        return EXIT_OK
    rows = store.list_records(apiary)
    open_rows = [r for r in rows if r.get("status") == store.STATUS_OPEN]
    print(f"{len(rows)} call(s) on record, {len(open_rows)} open")
    for meta in open_rows[:20]:
        print(_row(meta))
    prefix = session_prefix(args.session_id)
    if prefix:
        cfg = store.load_config()
        print(
            f"this session: {auto_call_count(prefix)} autonomous call(s) of "
            f"{cfg.get('max_autonomous_calls_per_session')}, "
            f"grant={'yes' if read_grant(prefix) else 'no'}"
        )
    return EXIT_OK


def cmd_show(args) -> int:
    try:
        apiary = _apiary_or_fail(args)
        path, meta, body = store.load(args.call_id, apiary)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        return _fail(str(exc))
    print(_row(meta))
    if meta.get("issue"):
        print(f"issue: {meta['issue']}")
    print(f"record: {path}")
    for item in store.parse_exchanges(body):
        print(f"\n--- exchange {item['number']} ---")
        print(f"sent: {item['message'][:400]}")
        reply = item["reply"]
        print(f"reply: {reply[:SHOW_REPLY_CHARS]}")
        if len(reply) > SHOW_REPLY_CHARS:
            print(f"... {len(reply) - SHOW_REPLY_CHARS} more characters in {path}")
    return EXIT_OK


def cmd_list(args) -> int:
    try:
        apiary = _apiary_or_fail(args)
    except RuntimeError as exc:
        return _fail(str(exc))
    rows = store.list_records(
        apiary, status=args.status, callee=args.callee, mode=args.mode, limit=args.limit
    )
    if not rows:
        print("no telephone records match")
        return EXIT_OK
    for meta in rows:
        print(_row(meta))
    return EXIT_OK


def cmd_hangup(args) -> int:
    try:
        apiary = _apiary_or_fail(args)
        path, meta, body = store.load(args.call_id, apiary)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        return _fail(str(exc))
    if meta.get("status") != store.STATUS_OPEN:
        print(f"{meta.get('id')} is already {meta.get('status')}; nothing to hang up")
        return EXIT_OK
    meta["status"] = store.STATUS_HUNG_UP
    meta["ended_at"] = now_iso()
    store.write_record(path, meta, body)
    print(f"{meta.get('id')} marked hung_up")
    pid = str(meta.get("pid") or "").strip()
    if args.kill and pid.isdigit():
        try:
            os.kill(int(pid), 15)
            print(f"sent a terminate signal to pid {pid}")
        except (OSError, ValueError) as exc:
            print(f"could not signal pid {pid}: {exc}")
    elif pid.isdigit():
        print(f"the caller process was pid {pid}. Re-run with --kill to signal it.")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telephone/cli.py",
        description="Place a Claude-to-Claude call into another registered repo",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--apiary-repo", default=None, help="Main-apiary checkout to use")
        p.add_argument("--session-id", default=None, help="Caller session id (default: detected)")
        return p

    p_call = common(sub.add_parser("call", help="Place a call to another registered repo"))
    p_call.add_argument("repo", help="Registry name of the callee, or a path to its checkout")
    p_call.add_argument("message", help="What to ask the callee")
    p_call.add_argument(
        "--act",
        action="store_true",
        help="Let the callee change files. Needs a user-typed /telephone grant",
    )
    p_call.add_argument(
        "--wait",
        action="store_true",
        help="Run in the foreground (the default). Say it to mean it",
    )
    p_call.add_argument("--model", default=None, help="Model for the callee run")
    p_call.add_argument("--timeout", type=int, default=None, help="Wall-clock limit in seconds")
    p_call.set_defaults(func=cmd_call)

    p_reply = common(sub.add_parser("reply", help="Send a follow-up on an existing call"))
    p_reply.add_argument("call_id", help="Call id, e.g. C-2026-1")
    p_reply.add_argument("message", help="The follow-up message")
    p_reply.add_argument("--wait", action="store_true", help="Run in the foreground (the default)")
    p_reply.add_argument("--model", default=None, help="Model for the callee run")
    p_reply.add_argument("--timeout", type=int, default=None, help="Wall-clock limit in seconds")
    p_reply.set_defaults(func=cmd_reply)

    p_status = common(sub.add_parser("status", help="Open calls, or one call's state"))
    p_status.add_argument("call_id", nargs="?", default=None, help="Call id to report on")
    p_status.set_defaults(func=cmd_status)

    p_show = common(sub.add_parser("show", help="Print one call's exchanges"))
    p_show.add_argument("call_id", help="Call id, e.g. C-2026-1")
    p_show.set_defaults(func=cmd_show)

    p_list = common(sub.add_parser("list", help="List calls on record, newest first"))
    p_list.add_argument("--limit", type=int, default=20, help="How many rows to print")
    p_list.add_argument("--status", default=None, help="Only calls with this status")
    p_list.add_argument("--callee", default=None, help="Only calls to this repo")
    p_list.add_argument("--mode", default=None, help="Only calls in this mode")
    p_list.set_defaults(func=cmd_list)

    p_hang = common(sub.add_parser("hangup", help="Close an open call's record"))
    p_hang.add_argument("call_id", help="Call id, e.g. C-2026-1")
    p_hang.add_argument("--kill", action="store_true", help="Also signal the recorded pid")
    p_hang.set_defaults(func=cmd_hangup)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
