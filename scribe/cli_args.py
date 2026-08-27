"""The scribe command line, declared in one place.

Split out of ``notes.py`` so ``build_parser()`` can be imported without
importing the handlers — the reference docs are meant to be generated from
argparse rather than hand-maintained (2026-08 review §5a-D), and
``docs/check_cli_claims.py`` already reconciles them against ``--help``.

Every subcommand's flags, defaults and help text are the CLI contract. Change
one and ``docs/reference/cli-tools.md`` has to change with it, or the push
gate says so.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe import infer, maintenance
from scribe.store import BRIEF_SUMMARY_MAX, VALID_TYPES


def _add_content_group(parser, *, required: bool) -> None:
    """``--content`` / ``--content-file``, mutually exclusive."""
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument('--content', default=None)
    group.add_argument('--content-file', dest='content_file', default=None,
                       help='Read content from a UTF-8 file instead of --content. Use '
                            'this when content contains backticks, /-prefixed tokens, '
                            'or filenames that would otherwise trigger shell command '
                            'substitution in the caller.')


def _add_role_mission(parser, *, filters: bool = False) -> None:
    """``--role`` / ``--mission``, as write metadata or as list filters."""
    if filters:
        parser.add_argument('--role', help='Filter by session role')
        parser.add_argument('--mission', help='Filter by session mission')
    else:
        parser.add_argument('--role', default='', help='Session role (e.g. user, attacker)')
        parser.add_argument('--mission', default='',
                            help='Session mission (e.g. general, project-x)')


def _add_infer_flags(parser) -> None:
    """The tag-inference opt-in pair, off by default.

    Off so a `/wrapup` handoff never spawns a model call; see
    :mod:`scribe.infer` for the precedence between these and the env var.
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--infer', action='store_true',
                       help='Infer --tags/--area via claude -p when neither is given. '
                            f'Off by default; {infer.INFER_ENV_VAR}=1 turns it on for '
                            'a whole session.')
    group.add_argument('--no-infer', dest='no_infer', action='store_true',
                       help=f'Never infer, even with {infer.INFER_ENV_VAR} set.')


def _add_id(parser, help_text='Note ID (e.g. T-2026-1)') -> None:
    parser.add_argument('id', type=str, help=help_text)


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI surface. One place, so `--help` is the spec."""
    parser = argparse.ArgumentParser(description='Scribe — structured note management')
    parser.add_argument('--project', default=None,
                        help='Project key (e.g. D--Professional-claude-apiary). '
                             'Defaults to cwd-derived key.')
    sub = parser.add_subparsers(dest='command')

    p_add = sub.add_parser('add')
    p_add.add_argument('--type', required=True, choices=VALID_TYPES)
    _add_content_group(p_add, required=True)
    p_add.add_argument('--summary', default='',
                       help='One-line abstract shown in lists and startup. '
                            'Required for --type handoff.')
    p_add.add_argument('--brief-summary', default='', dest='brief_summary',
                       help=f'One-sentence headline (<={BRIEF_SUMMARY_MAX} chars) for '
                            'GUI sidebar. Auto-derived from content if omitted.')
    p_add.add_argument('--session-id', default='')
    p_add.add_argument('--auto', action='store_true', help='Mark as auto-generated')
    p_add.add_argument('--if-no-handoff-for', default=None,
                       help='Only add if no handoff exists for this session ID')
    _add_role_mission(p_add)
    p_add.add_argument('--tags', default='',
                       help="Comma-separated tags stored on the note "
                            "(e.g. 'ticket:K-2026-99,priority:high').")
    p_add.add_argument('--unique-tag', default='', dest='unique_tag',
                       help='Add this tag only if no active note already carries it; '
                            'otherwise skip the add and exit 0.')
    p_add.add_argument('--force', action='store_true',
                       help="Bypass the template gate's required-section check "
                            '(logs a line to stderr naming what was missing).')

    p_template = sub.add_parser('template',
                                help='Inspect per-type formatting templates that gate `add`')
    p_template_sub = p_template.add_subparsers(dest='template_action')
    for action, blurb in (('show', "Print the template content for a type"),
                          ('path', "Print the absolute file path for a type's template")):
        p = p_template_sub.add_parser(action, help=blurb)
        p.add_argument('type', choices=VALID_TYPES)
    p_template_sub.add_parser('list',
                              help='List types whose template file exists and is non-empty')

    p_list = sub.add_parser('list')
    p_list.add_argument('--type', choices=VALID_TYPES)
    p_list.add_argument('--session')
    p_list.add_argument('--search')
    p_list.add_argument('--last', '--limit', type=int, dest='last',
                        help='Show only the N most recent matching notes '
                             '(--limit is an alias)')
    p_list.add_argument('--all', action='store_true',
                        help='Include done, dropped, and deferred notes')
    p_list.add_argument('--deferred', action='store_true', help='Show only deferred notes')
    p_list.add_argument('--archive', action='store_true', help='Search archive instead')
    _add_role_mission(p_list, filters=True)

    _add_id(sub.add_parser('get', aliases=['show']),
            'Note or learning ID (e.g. T-2026-1, L-2026-3)')
    for verb in ('done', 'drop', 'defer', 'resume', 'unarchive'):
        _add_id(sub.add_parser(verb))

    p_update = sub.add_parser('update')
    _add_id(p_update)
    _add_content_group(p_update, required=False)
    p_update.add_argument('--session-id', default=None)
    p_update.add_argument('--brief-summary', default='', dest='brief_summary',
                          help=f'Replace brief_summary (<={BRIEF_SUMMARY_MAX} chars).')
    p_update.add_argument('--add-tag', action='append', default=[], dest='add_tag',
                          help='Add a tag (repeatable). Idempotent; order-preserving.')
    p_update.add_argument('--remove-tag', action='append', default=[], dest='remove_tag',
                          help='Remove a tag (repeatable). Applied before --add-tag.')

    p_archive = sub.add_parser('archive')
    p_archive.add_argument('--before', help='Archive notes before this date (YYYY-MM-DD)')

    sub.add_parser('tidy',
                   help='Run the auto-archive retention sweep now (add and startup run it too)')
    sub.add_parser('mark-reviewed',
                   help='Stamp the learnings review timestamp the startup banner reads')

    p_learn = sub.add_parser('learn')
    _add_content_group(p_learn, required=True)
    p_learn.add_argument('--session-id', default='')
    p_learn.add_argument('--brief-summary', default='', dest='brief_summary',
                         help=f'One-sentence headline (<={BRIEF_SUMMARY_MAX} chars) for '
                              'GUI sidebar. Auto-derived if omitted.')
    _add_role_mission(p_learn)
    p_learn.add_argument('--tags', default='', help='Comma-separated tag list.')
    p_learn.add_argument('--area', action='append', default=[],
                         help='Area glob pattern (repeatable).')
    p_learn.add_argument('--supersedes', default='',
                         help='ID of a prior learning this one replaces (e.g. L-2026-5).')
    _add_infer_flags(p_learn)

    p_learnings = sub.add_parser('learnings')
    p_learnings.add_argument('--search')
    p_learnings.add_argument('--full', action='store_true',
                             help='Print full content (not truncated)')
    _add_role_mission(p_learnings, filters=True)
    p_learnings.add_argument('--tag', help='Filter by tag (substring match, case-insensitive)')
    p_learnings.add_argument('--area', help='Filter by area glob (exact match)')
    p_learnings.add_argument('--index', action='store_true',
                             help='Compact tag-grouped output for startup injection')

    _add_id(sub.add_parser('unlearn'), 'Learning ID (e.g. L-2026-3)')
    _add_id(sub.add_parser('archive-learning',
                           help='Archive a learning by ID (move to learnings/<year>/archive/)'),
            'Learning ID (e.g. L-2026-5)')

    p_supersede = sub.add_parser('supersede',
                                 help='Archive an existing learning and write a replacement')
    _add_id(p_supersede, 'ID of the learning to supersede (e.g. L-2026-5)')
    p_supersede.add_argument('--content', required=True,
                             help='Content of the replacement learning')
    p_supersede.add_argument('--content-file', dest='content_file', default=None,
                             help='Read the body from a file')
    p_supersede.add_argument('--session-id', default='')
    p_supersede.add_argument('--tags', default='', help='Comma-separated tag list.')
    p_supersede.add_argument('--area', action='append', default=[],
                             help='Area glob pattern (repeatable).')
    _add_role_mission(p_supersede)
    _add_infer_flags(p_supersede)

    p_repair = sub.add_parser('repair')
    p_repair.add_argument('--dry-run', action='store_true',
                          help='Report what would be fixed without modifying files')

    p_bf = sub.add_parser('backfill-brief',
                          help="Populate brief_summary on entries that don't have one yet")
    p_bf.add_argument('--dry-run', action='store_true',
                      help='Report what would change without writing')
    p_bf.add_argument('--force', action='store_true',
                      help='Re-derive brief_summary even for entries that already have one')

    p_retrotag = sub.add_parser('retrotag',
                                help='Infer tags and areas for learnings that have neither')
    p_retrotag.add_argument('--dry-run', action='store_true',
                            help='Print what would be tagged without writing')
    p_retrotag.add_argument('--model', default=None,
                            help='Override the claude model used for inference')
    p_retrotag.add_argument('--limit', type=int, default=None,
                            help='Process only the first N learnings (useful for spot-checks)')

    p_backup = sub.add_parser('backup',
                              help='Snapshot every index.jsonl to backups/<YYYY-MM-DD>/')
    p_backup.add_argument('--retain', type=int, default=maintenance.DEFAULT_RETAIN,
                          help=f'Dated snapshots to keep (default {maintenance.DEFAULT_RETAIN}; '
                               '0 keeps only the newest)')

    p_restore = sub.add_parser('restore',
                               help='Restore index.jsonl files from a dated snapshot')
    p_restore.add_argument('source', nargs='?', default=None, metavar='DATE',
                           help='Snapshot to restore (YYYY-MM-DD). Default: the newest.')
    p_restore.add_argument('--list', action='store_true',
                           help='List available snapshot dates and exit')
    p_restore.add_argument('--dry-run', action='store_true',
                           help='Report what would be restored without writing')

    return parser


# --------------------------------------------------------------------------- #
# Reading values back out of the parsed args
# --------------------------------------------------------------------------- #

def die(message: str) -> None:
    """Print an error to stderr and exit 1."""
    print(f'Error: {message}', file=sys.stderr)
    sys.exit(1)


def content_from_args(args):
    """Note content from ``--content-file`` if given, else ``--content``.

    ``--content-file`` is the list-form-subprocess rule made real: a caller
    with a multi-kilobyte body (an incubator spec, a /wrapup handoff) cannot
    put it on argv — Windows ``CreateProcess`` caps the command line at 32,767
    chars — and quoting it through a shell mangles backticks and ``/``-prefixed
    tokens.
    """
    content_file = getattr(args, 'content_file', None)
    if content_file is None:
        return args.content
    if not str(content_file).strip():
        die('--content-file needs a path (got an empty string).')
    try:
        return Path(content_file).read_text(encoding='utf-8')
    except OSError as e:
        die(f'cannot read --content-file {content_file!r}: {e}')


def check_length(value: str, cap: int, label: str, *, unit: str = 'chars') -> str:
    """Exit 1 when *value* is over *cap*; return it otherwise.

    ``unit='bytes'`` measures the UTF-8 encoding — that is the cap that keeps
    a runaway body out of the store; the ``chars`` caps are about how a
    summary reads in a list.
    """
    measured = len(value.encode('utf-8')) if unit == 'bytes' else len(value)
    if measured > cap:
        die(f'{label} exceeds {cap} {unit} ({measured}).')
    return value


def tag_list(args, attr: str = 'tags') -> list:
    """Split a comma-separated ``--tags`` value into a clean list."""
    raw = (getattr(args, attr, '') or '').strip()
    return [t.strip() for t in raw.split(',') if t.strip()] if raw else []
