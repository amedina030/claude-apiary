# Changelog

## Unreleased

### Hooks no longer vote on permissions (2026-08-26)

`core/hook_context.hook_allow` — the "nothing to object to" response every
apiary hook prints — emitted `permissionDecision: "allow"` on every call. In
Claude Code that is an auto-approve: any tool call a PreToolUse hook sees is
run without a prompt, so every bootstrapped repo had default-mode permission
prompts silently disabled (review C-1). Verified before/after with
`scripts/probe_permission_prompt.py` in a bootstrapped repo in `manual`
mode: an unlisted `python -c` ran before the fix and is denied (prompted)
after it.

- `hook_allow` now prints only `additionalContext` when it has one and `{}`
  otherwise — no permission field. A hook that really means to decide passes
  `decision="ask"|"deny"|"allow"` explicitly; anything else raises so a typo
  cannot become a vote. `hook_block` (deny + exit 2) is unchanged.
- `core/test_hook_context.py` covers the helpers and guards every
  `*/hooks/*.py` against a hand-rolled allow vote.
- If the returning prompts are annoying, the sanctioned answer is
  `permissions.allow` rules in the apiary profile (see
  `/fewer-permission-prompts`), never a hook vote.

### Secret-scanning hardening (2026-08-26)

A repo-wide review found both gates leakier than their docs claimed. Fixed:

- **The generic `key = value` rule never fired inside a `_`-joined name.** A
  `\b` cannot match between `secret` and `_access` in `aws_secret_access_key`,
  so the single most-leaked credential shape passed both gates. The key may
  now carry prefixes and suffixes (`DB_PASSWORD`, `my_password_value`), and
  suffixes that name something *about* a credential (`password_file`,
  `token_url`, `secret_name`) are excluded instead. Quoted values may contain
  any character, so passwords with punctuation are caught; indirection
  (`get_config()`, `${VAR}`) is judged on the value, not on a trailing comment.
- **One shared generic rule.** The push gate's Shannon-entropy bar was
  mathematically unreachable for any value under 16 characters and for hex
  keys of any length, while placeholders like `your_api_key_here` sit *above*
  real 16-char keys. Entropy is now only a floor for repetitive filler; both
  gates use the same placeholder / indirection / credential-signal filters in
  `core/secret_patterns.py`, and the parity suite covers the generic rule too.
- **New literal rules:** AWS secret keys (by key name — they have no prefix),
  GitHub fine-grained PATs, GitLab, Stripe, npm, PyPI, SendGrid, Slack
  webhooks, Twilio, JWTs, Azure storage keys; `ENCRYPTED` and `PGP … BLOCK`
  private-key headers.
- **The push gate scans every outgoing commit individually**, on the ref
  actually being pushed (honouring `git -C`, `--all`, refspecs), against the
  named remote. A secret committed and deleted two commits later is in the
  history being pushed and is now reported with its commit; the old cumulative
  `base..HEAD` diff could not see it.
- **Fail closed where it matters.** The commit gate's `_git()` returned `""`
  on any error, so a locked index or missing `git` produced "clean" — it now
  exits 2 and says the scan did not run. The push gate blocks (with the
  reason) when git times out or errors mid-scan; it still fails open only on
  internal errors before the scan starts.
- **Findings no longer reprint the credential.** The commit gate's `_redact`
  truncated at 100 characters — longer than every real key. Both gates now
  show `abcd…yz (n chars)` plus the key name.
- **Hook blocks are real blocks.** `core/hook_context.hook_block` emitted
  `permissionDecision: "block"`, which is not in Claude Code's documented
  `allow|deny|ask` vocabulary; it now emits `deny` with
  `permissionDecisionReason`, the legacy top-level `decision`/`reason` pair,
  and exits 2 with the reason on stderr.
- `.secretsallow` is now honoured by the push gate too (it previously read
  only the inline pragma, so the scanner's own fixture files could not be
  pushed). Entries are path rules unless prefixed `line:`; more
  credential-by-convention filenames are blocked (`*.key`, `.netrc`,
  `.git-credentials`, `kubeconfig`, `service-account*.json`, …); git output is
  read with `core.quotepath=false`/`--no-ext-diff`/`--no-textconv` and quoted
  paths are unquoted.

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
- **`core/secret_patterns.py`** — the literal-credential table, shared by the
  commit-time and push-time gates so they cannot drift apart. Each gate keeps
  its own generic `key = value` heuristic: the push gate bars on entropy, the
  commit gate filters placeholders and prose. Both honour both allowlist
  spellings, and a parity suite asserts they agree on every rule.

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
