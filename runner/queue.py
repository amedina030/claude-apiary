#!/usr/bin/env python3
"""List runner/* branches ready for review, joined with run_history.jsonl entries."""
from __future__ import annotations

import json
import sys

from .detached_lib import list_unmerged_runner_branches
from .run_history import RUN_HISTORY_FILE
from .target_repo import backlog_dir, hardens_dir, intake_dir, resolve_target_repo

INTAKE_DIR = intake_dir()
BACKLOG_DIR = backlog_dir()
HARDENS_DIR = hardens_dir()


def load_harden_verdict(uuid: str) -> str:
    """Return the harden verdict for a runner run, or 'n/a' if the artifact
    is missing. Distinguishes defender_failed runs from plain has_unresolved."""
    if not uuid:
        return 'n/a'
    p = HARDENS_DIR / f'{uuid}.json'
    if not p.exists():
        return 'n/a'
    try:
        return json.loads(p.read_text(encoding='utf-8')).get('verdict', 'unknown')
    except (OSError, json.JSONDecodeError):
        return 'unknown'

def load_run_history_entries() -> dict:
    """Return dict mapping run uuid -> latest entry dict. Missing file -> {}.

    Keyed by uuid, not by branch string (T-2026-278c). The old branch join
    silently produced a table of `unknown` in every column whenever the
    recorded branch name and the live branch name differed by so much as a
    slug — which, before one-branch-per-run, was every single detached run
    (review runner Bug 3). The uuid is in both, so it is the stable key.
    """
    if not RUN_HISTORY_FILE.exists():
        return {}
    result = {}
    try:
        for line in RUN_HISTORY_FILE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            uid = entry.get('uuid')
            if uid:
                result[uid] = entry  # later entries overwrite earlier
    except OSError:
        return {}
    return result


def uuid_for_branch(branch: str, entries: dict) -> str:
    """Find the run uuid a `runner/*` branch belongs to.

    Both naming conventions the runner has produced embed the uuid —
    `runner/<uuid>` and `runner/<slug>-<uuid>` — so a suffix match against
    the known uuids identifies the run without needing the branch string to
    round-trip exactly.
    """
    if not branch:
        return ''
    tail = branch[len('runner/'):] if branch.startswith('runner/') else branch
    for uid in entries:
        if tail == uid or tail.endswith(f'-{uid}'):
            return uid
    return ''

def load_ticket_summary(uuid: str) -> tuple:
    """Return (title, summary_line). Look in intake/<uuid>.json, then backlog/<uuid>.json. Defaults to ('unknown','')."""
    for d in (INTAKE_DIR, BACKLOG_DIR):
        p = d / f'{uuid}.json'
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding='utf-8'))
                title = data.get('title') or 'unknown'
                desc = data.get('description') or data.get('problem') or ''
                if isinstance(desc, dict):
                    desc = desc.get('problem', '') or ''
                summary = str(desc).splitlines()[0][:80] if desc else ''
                return title, summary
            except (OSError, json.JSONDecodeError):
                pass
    return 'unknown', ''

def main() -> int:
    entries = load_run_history_entries()
    branches = list_unmerged_runner_branches(resolve_target_repo())
    if not branches:
        print('No runner/* branches ready for review.')
        return 0
    # Header
    header = ['BRANCH', 'TICKET', 'STAGES', 'TOKENS', 'STATUS', 'HARDEN', 'SUMMARY']
    rows = []
    for b in sorted(branches):
        uuid = uuid_for_branch(b, entries)
        entry = entries.get(uuid, {})
        title, summary = load_ticket_summary(uuid) if uuid else ('unknown', '')
        stages = str(entry.get('stages_completed', 'unknown'))
        tokens = str(entry.get('total_tokens', 'unknown'))
        status = str(entry.get('exit_status', 'unknown'))
        harden = load_harden_verdict(uuid)
        rows.append([b, title, stages, tokens, status, harden, summary])
    # Compute column widths
    widths = [max(len(h), max((len(r[i]) for r in rows), default=0)) for i, h in enumerate(header)]
    fmt = '  '.join('{:<' + str(w) + '}' for w in widths)
    print(fmt.format(*header))
    print(fmt.format(*['-' * w for w in widths]))
    for r in rows:
        print(fmt.format(*r))
    return 0

if __name__ == '__main__':
    sys.exit(main())
