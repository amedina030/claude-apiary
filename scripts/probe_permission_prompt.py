#!/usr/bin/env python3
"""Empirically check whether apiary hooks still let default-mode prompts happen.

Runs a headless ``claude -p`` in *repo* (a bootstrapped target) in ``manual``
permission mode and asks it to run one Bash command that is neither
auto-approved (read-only git / ls are) nor built-in-protected (``rm`` always
asks). In headless mode a prompt cannot be shown, so "would have prompted"
surfaces as an entry in the JSON result's ``permission_denials``:

    denied  -> prompts are alive (hooks did not vote allow)   -> exit 0
    ran     -> something auto-approved the call               -> exit 1
    inconclusive / probe could not run                        -> exit 2 / 3

Use it before and after any change to hook responses (hook_context,
the dispatcher). Costs one short Haiku call. The CLAUDECODE* env vars are
scrubbed so the child is not treated as a sub-invocation (which never
prompts).

    poetry run python scripts/probe_permission_prompt.py D:/path/to/bootstrapped-repo
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

PROBE_COMMAND = 'python -c "print(12345*2)"'
PROBE_ANSWER = "24690"


class ProbeError(RuntimeError):
    """The probe itself could not run — distinct from a FAIL verdict."""


def scrubbed_env() -> dict:
    return {
        k: v
        for k, v in os.environ.items()
        if not (k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_"))
    }


def run_probe(repo: str, model: str, timeout: int) -> dict:
    claude = shutil.which("claude")
    if not claude:
        raise ProbeError("claude CLI not found on PATH")
    cmd = [
        claude,
        "-p",
        "Use the Bash tool to run exactly this command, then reply with only its output: "
        + PROBE_COMMAND,
        "--permission-mode",
        "manual",
        "--output-format",
        "json",
        "--max-turns",
        "2",
        "--model",
        model,
    ]
    try:
        r = subprocess.run(
            cmd,
            cwd=repo,
            env=scrubbed_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"could not run claude: {exc}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise ProbeError(
            f"claude did not return JSON (exit {r.returncode}):\n{r.stdout[:800]}\n{r.stderr[:800]}"
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", help="bootstrapped repo to probe (its hooks run)")
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args(argv)

    try:
        d = run_probe(args.repo, args.model, args.timeout)
    except ProbeError as exc:
        print(f"ERROR — {exc}", file=sys.stderr)
        return 3
    denials = [x for x in d.get("permission_denials", []) if x.get("tool_name") == "Bash"]
    result = (d.get("result") or "").strip()
    print(f"repo: {args.repo}")
    print(f"command: {PROBE_COMMAND}")
    print(f"result: {result[:200]!r}")
    print(f"bash permission denials: {len(denials)}   cost_usd: {d.get('total_cost_usd')}")
    if denials:
        print("OK — the call was held for a prompt; hooks are not auto-approving.")
        return 0
    if PROBE_ANSWER in result:
        print("FAIL — the call ran without a prompt; something voted allow.")
        return 1
    print("INCONCLUSIVE — neither denied nor ran; inspect the result above.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
