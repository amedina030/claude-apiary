# Changelog

## Unreleased

### Commit-time secret scanning (2026-08)

Every apiary-managed repo can now block a *commit* that would introduce a
credential. This complements the push-time gate added in T-2026-241
(`core/hooks/pre_push_secret_scan.py`), which covers a different half of the
problem: that one is a Claude Code PreToolUse hook, so it never fires for a
commit made by hand in a terminal, and never fires at all in a repo with no
remote — precisely the case that motivated this work. Until now, main-apiary's
pre-commit hook checked only doc conformance and spawned repos got no git hooks
at all, leaving `.gitignore` and human diff review as the only protection at
commit time.

- **`scripts/secret_scan.py`** — stdlib scanner over the *staged* diff (added
  lines only), reporting file, line, and matched pattern. Covers PEM private
  keys, AWS/Anthropic/OpenAI/GitHub/Slack/Google keys, credentials in URLs, and
  a filtered generic `key = value` rule. Also blocks credential-by-convention
  filenames (`.env`, `id_rsa`, `*.pem`) even when `git add -f` bypasses
  `.gitignore`. `--path` runs an ad-hoc scan; `--entropy` adds high-entropy
  matching (off by default — noisy).
- **`core/git_hooks.py`** — installs the hook into any managed repo, called by
  `apiary install` on every bootstrap so the protection can't decay as new
  repos appear. Never clobbers a pre-commit hook it doesn't own.
  `scripts/install_git_hooks.py` is a thin CLI over it for retrofits and
  inspection.
- **main-apiary's own pre-commit** now chains doc-check *and* secret-scan.
  Re-running `scripts/install_repo_hooks.py` upgrades an older hook in place.
- **Escape hatches:** an inline `apiary:allow-secret` comment, a repo-root
  `.secretsallow` regex file, or `git commit --no-verify`.

Deliberately **not** built on `gitleaks`: it needs a per-machine binary, so a
fresh clone would silently skip the check. See `PORTABILITY.md`.

The per-repo hook **fails closed** — if main-apiary can't be reached the commit
is blocked with instructions, rather than passing unscanned. A security control
that quietly stops working is worse than one that is loudly broken.

### Per-repo install migration (2026-05)

Apiary moved from a single global install in `~/.claude/` to a fully
per-repo install. Each repo you want apiary in is now bootstrapped
individually via `apiary install --target <repo>`; sessions in
non-bootstrapped repos run as vanilla Claude Code with no apiary hooks,
no managed CLAUDE.md zone, and no budgeter logging.

See `docs/architecture/per-repo-install.md` for the architecture and
`SETUP.md` for the new install flow.

**Why:** the global install had four real problems — buggy hooks broke
every Claude Code session on the machine; opening `claude` anywhere
paid apiary's startup cost even in unrelated repos; a single global
pointer named one apiary checkout (no parallel-version dev); and the
spooky-action-at-a-distance behavior was hard to discover from inside
an unrelated repo.

#### Breaking changes

- `setup.py --global`, `setup.py --project-path`, and `setup.py --check`
  are gone. `setup.py` is now a redirect stub that exits 1 with a
  message pointing at the new commands. Bootstrap any repo you want
  apiary in: `poetry run apiary install --target <repo>` (run main-apiary
  itself via `apiary self-bootstrap`).
- The `~/.claude/apiary*` global state — `apiary.json`, `apiary_repos.json`,
  `apiary_launch.py`, `apiary_bootstrap.py`, `.install-manifest.json`,
  `apiary_gui/`, `apiary_gui_dev/`, `transcripts/`, `.session-history.json`,
  `.session-identity-*.json`, all four `<flag>-enabled` toggle files,
  the 16 apiary slash command files in `commands/` — has been deleted.
- Hooks now use the per-repo launcher
  (`$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py`) instead of
  `~/.claude/apiary_launch.py`. Slash commands updated to match.
- The `APIARY_STATE_LAYOUT=legacy` escape hatch was removed; the
  centralized `<main-apiary>/.repos/<slug>/` layout is the only one.
- `core/flags.py` no longer falls back to `~/.claude/<flag>-enabled` —
  flags are read/written from `<repo>/.claude/apiary/flags/<flag>-enabled`
  exclusively.
- The `core/apiary_launch.py`, `core/apiary_bootstrap.py`, and
  `core/utils/apiary_pointer.py` modules were removed.

#### New CLI

`apiary` is now the unified console_script (registered by
`pyproject.toml`). Subcommands: `install`, `uninstall`, `self-bootstrap`,
`doctor`, `mailbox`, `cascade-fix`, `version`. Run `poetry run apiary --help`.

#### New mechanics

- **Three pin files** per bootstrapped repo at `<repo>/.claude/apiary/`:
  `main-apiary-pointer.json`, `self-pointer.json`, `version.json`.
- **Drift detection** runs as a PreToolUse hook on every session open in
  a bootstrapped repo. Move-vs-copy classification per
  `MIGRATION-PLAN.md` §3.10.
- **Mailbox** at `<main-apiary>/.apiary/forwarding/<uid>.json` carries
  `update_path` / `register_copy` messages from bootstrapped repos to
  main-apiary. Single-consumer; main-apiary processes it on its own
  session open and on `apiary doctor mailbox --fix`.
- **Cascade-fix** propagates a main-apiary move to every bootstrapped
  repo's `main-apiary-pointer.json`. Wired into main-apiary's drift
  handler (uid=1 dispatch); also exposed as `apiary cascade-fix` and
  `apiary doctor pointers --fix`.
- **Versioned migrations** under `<main-apiary>/migrations/v<from>_to_v<to>.py`,
  kept indefinitely. `apiary update` chains them.
- **`apiary doctor`** runs read-only consistency checks across pointers,
  registry, mailbox, versions, orphans, duplicates, and unreachable repos.
  `--fix` is supported for `mailbox` and `pointers`.
- **`incubator`** auto-bootstraps newly-spawned side-project repos as a
  best-effort step (failures don't fail the spawn).
- **GUI state** moved from `~/.claude/apiary_gui/` to
  `<main-apiary>/.apiary/gui/`. `gui/paths.py` resolves via `__file__`
  so the GUI always reads from the apiary tree it shipped from.

#### Migration

The migration ran in six commits on the `per-repo-migration` branch
(see git log for `11b6d33`, `8c66b4e`, `0ddb06e`, `b5e877a`, `f61beb3`,
`2149090`). Per-machine state under `~/.claude/` was migrated by the
phase-3 scripts in `scripts/phase3_*.py` and cleaned up by
`scripts/phase5_cleanup_global.py`. Each script defaults to dry-run;
`--apply` writes.

If you're on a fresh clone you don't need to run those — just `poetry
install` and `poetry run apiary self-bootstrap`.
