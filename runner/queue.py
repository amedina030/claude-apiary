#!/usr/bin/env python3
"""List runner/* branches ready for review, joined with overnight.jsonl entries."""
from __future__ import annotations
import json, sys
from pathlib import Path
from .detached_lib import list_unmerged_runner_branches, OVERNIGHT_LOG, SCRIPT_DIR

INTAKE_DIR = SCRIPT_DIR / 'intake'
BACKLOG_DIR = SCRIPT_DIR / 'backlog'
HARDENS_DIR = SCRIPT_DIR / 'hardens'


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

def load_overnight_entries() -> dict:
    """Return dict mapping branch_name -> latest entry dict. Missing file -> {}."""
    if not OVERNIGHT_LOG.exists():
        return {}
    result = {}
    try:
        for line in OVERNIGHT_LOG.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            b = entry.get('branch')
            if b:
                result[b] = entry  # later entries overwrite earlier
    except OSError:
        return {}
    return result

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
    entries = load_overnight_entries()
    branches = list_unmerged_runner_branches()
    if not branches:
        print('No runner/* branches ready for review.')
        return 0
    # Header
    header = ['BRANCH', 'TICKET', 'STAGES', 'TOKENS', 'STATUS', 'HARDEN', 'SUMMARY']
    rows = []
    for b in sorted(branches):
        entry = entries.get(b, {})
        uuid = entry.get('uuid') or ''
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
