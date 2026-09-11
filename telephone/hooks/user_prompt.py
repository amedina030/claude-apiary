#!/usr/bin/env python3
"""UserPromptSubmit hook: record that the *user* asked for a telephone call.

The grant this writes is the whole authorization model. ``telephone/cli.py``
can be run by the model from Bash at any time, so the CLI cannot tell a call
the user asked for from one Claude decided to place on its own. The one thing
the model cannot fake is a hook firing on the user's own keystrokes, so this
hook writes a per-session grant file whenever the submitted prompt invokes
``/telephone``, recording whether the word ``act`` was in it.

Two consequences, both deliberate:

* **Act mode needs a grant.** No grant, no act call, and the CLI says so.
* **Failing open means failing closed.** If this hook raises, is never
  installed, or the session has no id, no grant file exists, and the CLI treats
  the call as autonomous. The dispatcher's fail-open behaviour therefore
  removes a capability rather than granting one.

The grant is consumed (read and deleted) by the first ``call`` or ``reply`` that
finds it, so one typed command buys exactly one privileged call.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.hook_context import HookResult, context_block, run_standalone  # noqa: E402
from core.session import SessionId  # noqa: E402
from core.utils.timeutil import now_iso  # noqa: E402

#: Flag file suffix. ``telephone/cli.py`` reads it back by the same name.
GRANT_SUFFIX = "telephone_grant"
CONTEXT_NAMESPACE = "telephone"

#: ``/telephone <repo> [act] <message>``. The command has to open the prompt,
#: so a message that merely mentions the word never writes a grant.
_INVOCATION_RE = re.compile(r"^\s*/telephone(?:\s+(?P<rest>.*))?$", re.IGNORECASE | re.DOTALL)

#: How the user asks for act mode: the bare word, or the flag the CLI takes.
_ACT_TOKENS = ("act", "--act")


def parse_invocation(prompt: str) -> dict | None:
    """``{"repo", "act", "message"}`` when *prompt* invokes ``/telephone``.

    Returns ``None`` for every other prompt. The repo is the first token after
    the command, and act mode is the token straight after it (or an ``--act``
    anywhere in the arguments).
    """
    match = _INVOCATION_RE.match(prompt or "")
    if match is None:
        return None
    rest = (match.group("rest") or "").strip()
    tokens = rest.split()
    repo = tokens[0] if tokens else ""
    remainder = tokens[1:]
    act = bool(remainder) and remainder[0].lower() in _ACT_TOKENS
    if act:
        remainder = remainder[1:]
    elif "--act" in [t.lower() for t in remainder]:
        act = True
        remainder = [t for t in remainder if t.lower() != "--act"]
    return {"repo": repo, "act": act, "message": " ".join(remainder)}


def write_grant(sid: SessionId, invocation: dict) -> Path:
    """Write the grant file for *sid* and return its path."""
    path = sid.flag_path(GRANT_SUFFIX)
    payload = {
        "act": bool(invocation.get("act")),
        "repo": invocation.get("repo") or "",
        "session_id": str(sid),
        "granted_at": now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def run(payload: dict) -> HookResult | None:
    """Write a grant when the user's prompt invokes ``/telephone``."""
    event = payload.get("hook_event_name")
    if event not in (None, "UserPromptSubmit"):
        return None
    invocation = parse_invocation(str(payload.get("prompt") or ""))
    if invocation is None:
        return None

    raw = str(payload.get("session_id") or "").strip()
    if not raw:
        return None
    try:
        sid = SessionId(raw)
    except ValueError:
        return None

    write_grant(sid, invocation)
    mode = "act" if invocation["act"] else "answer"
    repo = invocation.get("repo") or "the named repo"
    return HookResult(
        context=context_block(
            CONTEXT_NAMESPACE,
            f"the user asked for an {mode}-mode call to {repo}, so this turn holds one "
            "grant for that repo. It is spent by the first `telephone/cli.py call` or "
            "`reply` that runs against it, and it is the only thing that unlocks act mode.",
        )
    )


if __name__ == "__main__":
    run_standalone(run, event="UserPromptSubmit")
