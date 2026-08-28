#!/usr/bin/env python3
"""Generate the reference tables that are not about argparse.

Sibling to ``docs/generate_cli_docs.py`` (which owns the CLI tables). Five
sources of truth, five generated blocks:

===========================  ====================================  ==========================
Block                        Source of truth                       Document
===========================  ====================================  ==========================
``hooks:events``             ``core.hooks.dispatch.EVENTS``        ``docs/reference/hooks.md``
``hooks:registry``           ``core.hooks.dispatch._registry()``   ``docs/reference/hooks.md``
``slash-commands``           ``<tool>/commands/*.md`` frontmatter  ``docs/reference/slash-commands.md``
``config:<file>``            the shipped ``config.json`` files     ``docs/reference/config-files.md``
``storage:paths``            the real path resolvers               ``docs/reference/file-storage.md``
``scribe:archive-policy``    ``scribe/policy.py``                  ``scribe/CLAUDE.md``
===========================  ====================================  ==========================

Same contract as the CLI generator: the row *set* and the factual columns
(event, matcher, module, default value, path template) come from code; the
prose columns are carried over from the existing table and only seeded from
code when the doc has nothing to say yet. See ``docs/docgen.py``.

The storage table is produced by *calling the resolvers* against a synthetic
state directory and printing what comes back relative to it — so it cannot
drift from ``core.utils.state``, and it never leaks a machine-specific path
into a committed doc.

Usage::

    python docs/generate_reference.py            # --check (default)
    python docs/generate_reference.py --write
    python docs/generate_reference.py --check --diff
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent
for _p in (str(DOCS_DIR), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import docgen  # noqa: E402
from core import frontmatter  # noqa: E402

HOOKS_DOC = DOCS_DIR / "reference" / "hooks.md"
COMMANDS_DOC = DOCS_DIR / "reference" / "slash-commands.md"
CONFIG_DOC = DOCS_DIR / "reference" / "config-files.md"
STORAGE_DOC = DOCS_DIR / "reference" / "file-storage.md"
SCRIBE_DOC = REPO_ROOT / "scribe" / "CLAUDE.md"

#: Config files with a generated key table, in the order they appear in the doc.
CONFIG_FILES = ("runner/config.json", "budgeter/config.json", "compass/config.json")

STATE = "&lt;state-dir&gt;"
REPO = "&lt;repo&gt;"
MAIN = "&lt;main-apiary&gt;"


# --------------------------------------------------------------------------- #
# hooks.md
# --------------------------------------------------------------------------- #

def _dispatch():
    from core.hooks import dispatch
    return dispatch


def hook_event_records() -> list[docgen.Record]:
    dispatch = _dispatch()
    return [
        docgen.Record(key=event, cells={"Event": f"**{event}**",
                                        "Dispatcher verb": f"`{verb}`"})
        for verb, event in dispatch.EVENTS.items()
    ]


def hook_registry_records() -> list[docgen.Record]:
    """One row per registered hook, in the order the dispatcher runs them."""
    dispatch = _dispatch()
    registry = dispatch._registry()
    records: list[docgen.Record] = []
    for verb, event in dispatch.EVENTS.items():
        hooks = registry.get(event, ())
        for i, hook in enumerate(hooks, start=1):
            module = hook.module
            path = (module if module.endswith(".py")
                    else module.replace(".", "/") + ".py")
            matcher = hook.matcher or ""
            records.append(docgen.Record(
                key=f"{event}:{hook.name}",
                cells={
                    "Event": f"{event} (`{verb}`)",
                    "#": str(i),
                    "Hook": f"`{hook.name}`",
                    "Module": f"`{path}`",
                    "Matcher": f"`{matcher}`" if matcher else "_(every tool)_",
                },
            ))
    return records


def build_hooks(text: str) -> str:
    events = docgen.sync_table(
        docgen.first_table(docgen.block_body(text, "hooks:events") or ""),
        hook_event_records(),
        ["Event", "Dispatcher verb", "When it fires"],
        generated=["Event", "Dispatcher verb"],
        key_of_row=lambda row, _h: docgen.cell_key(row[0]).strip("*"),
    )
    text = docgen.set_block(text, "hooks:events", docgen.render_table(events))

    registry = docgen.sync_table(
        docgen.first_table(docgen.block_body(text, "hooks:registry") or ""),
        hook_registry_records(),
        ["Event", "#", "Hook", "Module", "Matcher", "What it does"],
        generated=["Event", "#", "Hook", "Module", "Matcher"],
        key_of_row=_registry_row_key,
    )
    return docgen.set_block(text, "hooks:registry", docgen.render_table(registry))


def _registry_row_key(row: list[str], headers: list[str]) -> str:
    """``<Event>:<hook name>`` for a row of the registry table."""
    def cell(name: str) -> str:
        return row[headers.index(name)] if name in headers and headers.index(name) < len(row) else ""
    event = cell("Event").split("(")[0].strip()
    return f"{event}:{docgen.cell_key(cell('Hook'))}"


# --------------------------------------------------------------------------- #
# slash-commands.md
# --------------------------------------------------------------------------- #

def command_records() -> list[docgen.Record]:
    """Every ``<tool>/commands/*.md``, named by its frontmatter ``name``."""
    records: list[docgen.Record] = []
    for path in sorted(REPO_ROOT.glob("*/commands/*.md")):
        fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
        name = (fm or {}).get("name") or path.stem
        rel = path.relative_to(REPO_ROOT).as_posix()
        records.append(docgen.Record(
            key=f"/{name}",
            cells={"Command": f"`/{name}`", "Source": f"`{rel}`",
                   "Description": str((fm or {}).get("description", ""))},
        ))
    records.sort(key=lambda r: r.key)
    return records


def build_commands(text: str) -> str:
    table = docgen.sync_table(
        docgen.first_table(docgen.block_body(text, "slash-commands") or ""),
        command_records(),
        ["Command", "Source", "Description"],
        generated=["Command", "Source"],
    )
    return docgen.set_block(text, "slash-commands", docgen.render_table(table))


# --------------------------------------------------------------------------- #
# config-files.md
# --------------------------------------------------------------------------- #

def _type_name(value) -> str:
    return {bool: "bool", int: "int", float: "float", str: "string",
            list: "array", dict: "object", type(None): "null"}.get(type(value), "any")


def _render_default(value) -> str:
    if isinstance(value, str):
        return f'`"{value}"`'
    return f"`{json.dumps(value)}`"


def config_records(rel: str) -> list[docgen.Record]:
    """Flatten one config.json into ``(section, field)`` rows.

    Keys starting with ``_`` are comments and are skipped. A two-level file
    (``runner/config.json``) yields Section+Field rows; a flat one yields
    Field rows with an empty Section.
    """
    data = json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))
    records: list[docgen.Record] = []
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and value and all(not k.startswith("_") for k in value):
            for sub, subval in value.items():
                records.append(docgen.Record(
                    key=f"{key}.{sub}",
                    cells={"Section": f"`{key}`", "Field": f"`{sub}`",
                           "Type": _type_name(subval),
                           "Default": _render_default(subval)},
                ))
            continue
        records.append(docgen.Record(
            key=key,
            cells={"Section": "", "Field": f"`{key}`", "Type": _type_name(value),
                   "Default": _render_default(value)},
        ))
    return records


def budgeter_extra_records() -> list[docgen.Record]:
    """The four pricing weights ``budgeter/report.py`` defaults in code.

    They are absent from the shipped ``budgeter/config.json`` on purpose, so
    the only place their defaults exist is the module — read them from there
    rather than restating the numbers in prose (the old table did, and one of
    them was already wrong).
    """
    import importlib
    report = importlib.import_module("budgeter.report")
    names = {
        "price_weight_input": "_DEFAULT_PRICE_WEIGHT_INPUT",
        "price_weight_cache": "_DEFAULT_PRICE_WEIGHT_CACHE",
        "price_weight_cache_creation": "_DEFAULT_PRICE_WEIGHT_CACHE_CREATION",
        "price_weight_output": "_DEFAULT_PRICE_WEIGHT_OUTPUT",
    }
    out: list[docgen.Record] = []
    for key, const in names.items():
        if not hasattr(report, const):
            continue
        value = getattr(report, const)
        out.append(docgen.Record(
            key=key,
            cells={"Section": "", "Field": f"`{key}`", "Type": _type_name(value),
                   "Default": _render_default(value) + " (code default; not in the file)"},
        ))
    return out


def build_config(text: str) -> str:
    for rel in CONFIG_FILES:
        key = f"config:{rel}"
        records = config_records(rel)
        if rel == "budgeter/config.json":
            records += budgeter_extra_records()
        headers = ["Section", "Field", "Type", "Default", "Description"]
        if not any(r.cells.get("Section") for r in records):
            headers = ["Field", "Type", "Default", "Description"]
        table = docgen.sync_table(
            docgen.first_table(docgen.block_body(text, key) or ""),
            records, headers,
            generated=[h for h in headers if h != "Description"],
            key_of_row=_config_row_key,
        )
        text = docgen.set_block(text, key, docgen.render_table(table))
    return text


def _config_row_key(row: list[str], headers: list[str]) -> str:
    def cell(name: str) -> str:
        if name not in headers:
            return ""
        i = headers.index(name)
        return row[i] if i < len(row) else ""
    section = docgen.cell_key(cell("Section"))
    field = docgen.cell_key(cell("Field"))
    return f"{section}.{field}" if section else field


# --------------------------------------------------------------------------- #
# file-storage.md
# --------------------------------------------------------------------------- #

def _rel_to(path: Path, base: Path, label: str) -> str:
    """``<label>/tail`` when *path* is under *base*, else the raw path."""
    try:
        tail = Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return f"`{Path(path).as_posix()}`"
    return f"`{label}/{tail}`" if tail != "." else f"`{label}/`"


def storage_records() -> list[docgen.Record]:
    """Probe every state resolver against a synthetic target and record what
    it returns, relative to the state dir / repo / main-apiary root."""
    from core.utils import state as state_mod

    rows: list[tuple[str, str, str]] = []   # (key, path, resolver)
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        state = base / "state"
        repo = base / "repo"
        state.mkdir(parents=True, exist_ok=True)
        repo.mkdir(parents=True, exist_ok=True)
        saved = os.environ.get(state_mod.TARGET_STATE_DIR_ENV)
        os.environ[state_mod.TARGET_STATE_DIR_ENV] = str(state)
        try:
            import captures.store as captures_store
            import compass.store as compass_store
            import researcher.store as researcher_store
            import scribe.paths as scribe_paths
            from core import flags as core_flags
            from core import session as core_session
            from core.hooks import dispatch
            from runner import target_repo

            rows += [
                ("scribe", _rel_to(scribe_paths.scribe_state_dir(repo), state, STATE),
                 "`scribe.paths.scribe_state_dir`"),
                ("compass", _rel_to(compass_store.compass_dir(repo), state, STATE),
                 "`compass.store.compass_dir`"),
                ("research", _rel_to(researcher_store.research_dir(repo), state, STATE),
                 "`researcher.store.research_dir`"),
                ("captures", _rel_to(captures_store.captures_dir(repo), state, STATE),
                 "`captures.store.captures_dir`"),
                ("sessions", _rel_to(core_session.sessions_dir(warn=False), state, STATE),
                 "`core.session.sessions_dir`"),
                ("runner", _rel_to(target_repo.artifacts_root(), state, STATE),
                 "`runner.target_repo.artifacts_root`"),
            ]
            for name in ("intake", "backlog", "specs", "plans", "executions",
                         "hardens", "reports", "locks", "runs", "logs"):
                fn = getattr(target_repo, f"{name}_dir", None)
                if fn is None:
                    continue
                rows.append((f"runner/{name}", _rel_to(fn(), state, STATE),
                             f"`runner.target_repo.{name}_dir`"))
            rows.append(("runner/run_history",
                         _rel_to(target_repo.run_history_path(), state, STATE),
                         "`runner.target_repo.run_history_path`"))
            # Worktrees are the one runner artifact that is NOT state: a git
            # worktree has to live next to the repo it is cut from.
            rows.append(("runner worktrees",
                         _rel_to(target_repo.worktrees_dir(repo), repo, REPO),
                         "`runner.target_repo.worktrees_dir`"))

            saved_project = os.environ.get("CLAUDE_PROJECT_DIR")
            os.environ["CLAUDE_PROJECT_DIR"] = str(repo)
            try:
                flag = _rel_to(core_flags._flag_path("NAME"), repo, REPO)
                rows.append(("flags", flag.replace("NAME", "&lt;flag&gt;"),
                             "`core.flags._flag_path`"))
                rows.append(("hooks.log", _rel_to(dispatch.log_path(), repo, REPO),
                             "`core.hooks.dispatch.log_path`"))
            finally:
                if saved_project is None:
                    os.environ.pop("CLAUDE_PROJECT_DIR", None)
                else:
                    os.environ["CLAUDE_PROJECT_DIR"] = saved_project

            rows += [
                ("self-pointer", _rel_to(state_mod.self_pointer_path(repo), repo, REPO),
                 "`core.utils.state.self_pointer_path`"),
                ("main-apiary-pointer",
                 _rel_to(state_mod.main_apiary_pointer_path(repo), repo, REPO),
                 "`core.utils.state.main_apiary_pointer_path`"),
                ("version pin", _rel_to(state_mod.version_path(repo), repo, REPO),
                 "`core.utils.state.version_path`"),
                ("pointer breadcrumb",
                 f"`{REPO}/{state_mod.POINTER_DIRNAME}/{state_mod.POINTER_FILENAME}`",
                 "`core.utils.state` constants"),
                ("registry", _rel_to(state_mod.registry_path(repo), repo, MAIN),
                 "`core.utils.state.registry_path`"),
                ("next_id", _rel_to(state_mod.next_id_path(repo), repo, MAIN),
                 "`core.utils.state.next_id_path`"),
                ("per-target state root",
                 f"`{MAIN}/{state_mod.REPOS_DIRNAME}/&lt;name&gt;-&lt;uid&gt;/`",
                 "`core.utils.state.resolve_target_state_dir`"),
            ]

            import gui.paths as gui_paths
            rows.append(("gui", _rel_to(gui_paths.state_dir(), gui_paths.main_apiary(), MAIN),
                         "`gui.paths.state_dir`"))

            # `LOG_PATH` / `TMP_DIR` are module globals that
            # `configure_for_project()` rebinds, so reading them here would
            # print whatever the last caller in this process redirected them
            # to — a pytest tmpdir, when the generator runs inside the suite.
            # `_DEFAULT_*` are captured at import and never reassigned; they
            # are what the doc means by "where the budgeter writes".
            from budgeter.lib import logger as budgeter_logger
            rows.append(("budgeter log",
                         _rel_to(budgeter_logger._DEFAULT_LOG_PATH, REPO_ROOT, MAIN),
                         "`budgeter.lib.logger._DEFAULT_LOG_PATH`"))
            rows.append(("budgeter baselines",
                         _rel_to(budgeter_logger._DEFAULT_TMP_DIR, REPO_ROOT, MAIN),
                         "`budgeter.lib.logger._DEFAULT_TMP_DIR`"))
        finally:
            if saved is None:
                os.environ.pop(state_mod.TARGET_STATE_DIR_ENV, None)
            else:
                os.environ[state_mod.TARGET_STATE_DIR_ENV] = saved

    return [docgen.Record(key=key, cells={"What": key, "Path": path,
                                          "Resolved by": resolver})
            for key, path, resolver in rows]


def build_storage(text: str) -> str:
    table = docgen.sync_table(
        docgen.first_table(docgen.block_body(text, "storage:paths") or ""),
        storage_records(),
        ["What", "Path", "Resolved by"],
        generated=["What", "Path", "Resolved by"],
        key_of_row=lambda row, _h: (row[0] if row else "").strip("` "),
    )
    return docgen.set_block(text, "storage:paths", docgen.render_table(table))


# --------------------------------------------------------------------------- #
# scribe/CLAUDE.md — archive policy
# --------------------------------------------------------------------------- #

def archive_policy_records() -> list[docgen.Record]:
    """The retention rules, read out of ``scribe/policy.py``'s own constants."""
    import scribe.policy as policy

    rows = [("handoff", "A newer handoff for the same `(role, mission)` exists")]
    for note_type, days in policy._AGE_RULES.items():
        rows.append((note_type, f"{days} day{'s' if days != 1 else ''} old"))
    rows.append((
        "*(any type)* marked `done`",
        f"{policy.DONE_RETENTION_DAYS} day"
        f"{'s' if policy.DONE_RETENTION_DAYS != 1 else ''} after it was "
        f"**marked done** (`status_changed_at`), not after it was written",
    ))
    never = sorted(_never_archived(policy))
    rows.append((", ".join(f"`{t}`" for t in never),
                 "Never on age — only once closed"))
    return [docgen.Record(key=_policy_key(what),
                          cells={"Type": what if what.startswith(("*", "`")) else f"`{what}`",
                                 "Archived when": when})
            for what, when in rows]


def _never_archived(policy) -> set[str]:
    """Note types with no age rule, taken from the scribe store's type list."""
    from scribe.store import VALID_TYPES
    aged = set(policy._AGE_RULES) | {"handoff"}
    return {t for t in VALID_TYPES if t not in aged}


def _policy_key(what: str) -> str:
    return what.replace("`", "").replace("*", "").strip()


def build_archive_policy(text: str) -> str:
    table = docgen.sync_table(
        docgen.first_table(docgen.block_body(text, "scribe:archive-policy") or ""),
        archive_policy_records(),
        ["Type", "Archived when"],
        generated=["Type", "Archived when"],
        key_of_row=lambda row, _h: _policy_key(row[0] if row else ""),
    )
    return docgen.set_block(text, "scribe:archive-policy", docgen.render_table(table))


# --------------------------------------------------------------------------- #

def generators() -> list[docgen.Generator]:
    return [
        docgen.Generator(HOOKS_DOC, build_hooks),
        docgen.Generator(COMMANDS_DOC, build_commands),
        docgen.Generator(CONFIG_DOC, build_config),
        docgen.Generator(STORAGE_DOC, build_storage),
        docgen.Generator(SCRIBE_DOC, build_archive_policy),
    ]


def main(argv: list[str] | None = None) -> int:
    return docgen.run_generators(
        generators,
        description="Generate the non-argparse reference tables from code",
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
