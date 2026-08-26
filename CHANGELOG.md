# Changelog

## Unreleased

### Phase 1 — unbreak what was silently broken (2026-08-26)

- **Apiary writes nothing under `~/.claude` — now true.** Session identity
  and the session history / last-session records live under
  `<main-apiary>/.repos/<slug>/sessions/` (the launcher's
  `APIARY_TARGET_STATE_DIR`), once-per-session hook flags under
  `<repo>/.claude/apiary/session-tmp/` (created by the installer,
  git-ignored); with neither resolvable the fallback is the OS temp dir,
  announced once on stderr. The GUI's `bubble_anomalies.jsonl` moves under
  the GUI state dir too, and `find_state_dir` now reads the live
  `.claude/apiary/*-pointer.json` pins (the retired `.apiary/pointer` it
  looked for meant launcher-less callers could never find their state).
  `core/session.py` used to anchor both at `~/.claude` and grew ~5 stray
  files per session (review S1). `save_transcript.py` — a Stop hook that
  runs every turn — no longer crashes on a lock timeout or an unwritable
  dir (Bug 11). `core/test_session.py` pins the layout; the hook tests
  point every hook at temp repo/state dirs.
- The two scheduled tasks (`overnight-runner`, `compass-weekly-synthesis`)
  had drifted to a bare `python` that does not resolve to the poetry env
  under Task Scheduler; `cron_health repair --apply` recreated them with
  the venv interpreter after confirming the registered commands (1.4).
  Compass synthesis keeps running per §6 (keep, fix, measure).
- **`/budgeter-log`, `/budgeter-warn`, `/budgeter-session-warn` toggled the
  wrong file** (`~/.claude/<flag>-enabled`) while the hooks read
  `<repo>/.claude/apiary/flags/<flag>-enabled` — they reported ON and changed
  nothing (B4). Replaced by one `/budgeter <log|warn|session-warn>` that
  shells out to a new `core/flags.py` CLI (`toggle|enable|disable|status
  <name>`, prints ON/OFF, exit 0/1), so the toggle and the hooks share one
  code path. Already-bootstrapped repos keep the three stale command files
  until re-bootstrapped (`apiary install` does not prune — tracked).
- **`apiary doctor <check> --fix` works.** The console script never declared
  `--fix`, so the one remediation the docs and doctor's own messages point at
  exited 2. `core/cli.py` gets its first tests (29) — every verb's argv, and
  the `--fix` seam through the real `doctor.main`.
- `docs/check_cli_claims.py` reconciles console scripts (`CONSOLE_SCRIPTS`
  maps `apiary` → `core/cli.py`), so the `apiary` section is checked like
  every other tool; its cli-tools.md section was rewritten to match
  (Subcommands/Flags tables, the missing `doctor stale`, a first-run
  prompt / `--force` narrative that never existed removed). The checker now
  also runs in the docs pre-commit hook next to `docs/check.py`
  (re-run `python scripts/install_repo_hooks.py` to pick it up).
- **Scribe: mutations on archived notes actually write now.** `update_note`
  searches the year's `archive/` index when the active index misses, and
  `done`/`drop`/`defer`/`resume`/`update` exit 1 instead of printing false
  success. Done notes auto-archive one day after being *marked* done
  (`status_changed_at`), not one day after creation. `list` no longer
  archives as a side effect — that sweep is the new `notes.py tidy` — and
  `notes.py mark-reviewed` stamps the learnings review marker that
  `/review-learnings` was writing to the wrong directory (`/notes learning`
  → `learnings` fixed too).
- **Scribe note templates, one per type (§5a-B, option C).** Bootstrap seeds
  `<state-dir>/scribe/templates/` from `scribe/default_templates/`, never
  overwriting. `handoff`, `decision` and `blocker` enforce their required
  sections on `add` (handoff matches `/wrapup`'s structure, pinned by a
  test); the other five types are guidance only. The `--ack-template` hash
  handshake is gone — one check, one attempt, `--force` bypasses and logs
  what it skipped. Existing notes are never validated or rewritten.
- **`researcher/_yaml_mini` corrupted frontmatter on every read.** `dumps`
  quoted ambiguous values but `loads` never unquoted them, and any `#`
  started a comment, so `/research verify` (load → mutate → dump) degraded
  any title with a colon or URL with a fragment, compounding each pass.
  Quotes are now unquoted only when symmetric, `#` opens a comment only at
  line start or after whitespace; round-trip tests cover the failing inputs.
  Captures inherits the fix.
- **Incubator no longer passes the `/refine` spec on argv** (B9): it stages
  the body to a temp file and uses `scribe/notes.py --content-file` (now on
  `learn` too), so multi-kilobyte specs migrate instead of dying at the
  Windows command-line cap. Spawn's git/scribe `OSError`s are reported (exit
  5) not raised; the partial-failure recovery hint says "close the original"
  instead of duplicating the spec (B10); spawned-repo templates drop the
  crashing `report.py --since 7d` example and the dead `.apiary/` gitignore
  rules.
- **Runner: the monolithic executor stamps `schema_version`** on its artifact
  and asserts the plan's, so stage 5 no longer rejects everything stage 4
  produced. Stage 4's default flips back to the per-step `executor`:
  `executor.mode` was introduced 2026-04-14, one day after the last
  runner-produced commit, so the monolithic path has never completed a run.
  A test drives stage 4 into `auto_harden.main` over a real temp repo.
- **Compass:** `backfill.py` stamps `captured_at` from the transcript's mtime
  instead of the backfill time (restoring the recency ordering synthesis
  weights on); `synthesize.py` caps its prompt at the 50 most recent sessions
  (`--max-sessions`, 0 = no cap) and writes `personality.md` atomically. The
  tmp+replace helper is now shared as `core/utils/atomic.py` with budgeter's
  two copies pointed at it (four more copies remain for Phase 3).

### Runner never pushes, never sweeps, never runs unbounded (2026-08-26)

Review runner Bug 9 and the permissions note.

- `runner/approval.py` no longer pushes. A fully-resolved run is
  squash-merged to master locally, reported as `merged-locally`, and a todo
  asks the operator to review and push. The unattended push from the
  interactive checkout was the worst path in the package.
- `auto_harden.commit_all` uses `git add -u` plus the round's declared files
  instead of `git add -A`, which swept the operator's untracked scratch
  files into "harden round fixes" commits.
- `claude_subprocess.run_claude` passes `--disallowedTools` for the
  `git push` and `gh pr merge/create` command families and `--max-turns
  150` on every stage call, so a subprocess cannot push through the Bash
  tool — a deny beats an allow at every settings level, verified against
  `claude -p --allowedTools "Bash(git push *)"` — nor loop until the
  timeout. Known limits, stated rather than papered over: `git -c k=v push`
  / `git --no-pager push` are not matched (a `git * push *` rule was tried
  and rejected — it also denied `git log --grep push`, breaking the
  executor's own commit step), and permission rules cannot see a push
  issued from a script file or another interpreter.
- **The runner now brings its own tool grant.** Until this PR every
  headless stage worked only because the apiary hooks auto-approved every
  tool call (C-1); with that gone, `claude -p` denied every Edit/Write/Bash
  and step 1 failed with "no changes". `run_claude` passes
  `--permission-mode acceptEdits` and `--allowedTools` Read/Edit/Write/
  Glob/Grep + `Bash(git|python|poetry|pytest *)` — narrower than the vote
  ever was — configurable under `subprocess` in `runner/config.json`. The
  runner revive programme's e2e test is where that list gets validated. When `claude -p` stops for a reason it only reports in
  its JSON (`error_max_turns`, …) it exits 1 with empty stderr; `run_claude`
  now puts that reason into the returned stderr so stage logs say why.

### GUI: no lost messages on attach, no raw Ctrl+C, no orphaned claude (2026-08-26)

Review gui #2/#3/#4/#12.

- **Transcript attach race.** `Session._start_tail` read the file, replayed
  it, and *then* fast-forwarded the tail to the file's current size — every
  record claude appended in between (routinely, while streaming its first
  turn) was never rendered until `Ctrl+R`. It now reads the bytes once and
  starts the tail at exactly that byte count (`TranscriptTail(start_at=)`).
  The tail is byte-mode (a text-mode seek to a byte offset was undefined)
  and reparses from the top when the file shrinks.
- **Raw Ctrl+C is refused by the backend.** The "never send `\x03`" rule
  lived in a JS comment; `send_text`/`send_control`/`send_bytes`/`send_input`
  now return False for it at both the `Session` and `PtyWrapper` layers.
  Interrupt is still ESC then Ctrl+U.
- **Closing a tab closes the pty and kills the tree.** `PtyWrapper.stop`
  only terminated the direct child; on npm installs that is the `cmd /c`
  shim and the node grandchild running Claude Code lived on, holding the
  pty's pipe, burning quota and competing with the restarted session's
  JSONL. `stop` now kills the process tree first (`taskkill /T` or
  `killpg`, capability-detected — while the direct child is still alive so
  its children can be enumerated), then terminates, `close(force=True)`s
  the pty (which also unblocks the reader thread) and joins the reader. A
  Windows-only integration test spawns `cmd /c python` through a real pty
  and checks the grandchild is gone; it caught the wrong ordering. The
  reader thread is unblocked by `shutdown()`ing pywinpty's read socket
  (its `close()` alone never wakes a blocked `recv`), a deliberate stop no
  longer fires the "exited (code -1)" toast, and `taskkill` runs without a
  console window in the packaged build. The attach replay stops at the
  last complete line so a record torn mid-write is picked up whole by the
  tail instead of being half-dropped. Ctrl+C typed in the xterm pane is
  routed to the ESC+Ctrl+U interrupt instead of being silently dropped.

### Budgeter hooks: no crashes, honest counts (2026-08-26)

Review B1/B2/B3/B5/B6. The three budgeter hooks measured the wrong thing
and could break a session for good:

- **A truncated baseline wedged the session.** `save_baseline` wrote in
  place, `load_baseline` raised on bad JSON before anything could rewrite
  it, and no hook wrapped `main()` — so one hook killed mid-write meant a
  hook error on every later monitored call. Baselines are now written via
  temp file + `os.replace`, an unreadable one is treated as absent (one
  stderr line, then rewritten), all three hooks catch everything, and the
  Stop hook always reaches `cleanup_session`.
- **Multi-block turns were counted 2-3x.** Claude Code writes one JSONL
  line per content block with the same `message.id` and `usage`; the
  cumulative and last-call figures now dedupe on `message.id`.
- **`cache_creation_input_tokens` was never counted.** It is now part of
  the cumulative total, the last-call split (`baseline_cache_creation`,
  `cache_creation_tokens_delta`), the session-length nudge, and
  `report.py --weighted` (new `price_weight_cache_creation`, default 1.25).
  `net_tokens_delta` sums the prompt components before clamping so tokens
  that move from "written" to "read" between calls net to zero.
- **Parallel tool calls produced phantom entries** (25% of the log). A PRE
  that sees no API call since the previous one now logs nothing.
- **Agent payloads over 64 KB were dropped silently** — exactly the most
  expensive calls. `post_tool_use.py` reads the whole payload and says so
  on stderr when it can't parse it.
- Baselines carry a `schema` (now 2). A baseline written by the old
  per-line counting is 1.7–2.6× too large to compare against, so the first
  PRE after upgrade used to log a spurious `[compaction]` marker and drop an
  entry; old-schema baselines are kept for turn continuity but never
  compared. The Stop hook no longer logs against a shrunk total either.

### GUI permission gate fails closed (2026-08-26)

The opt-in MCP permission server (`gui/permission_mcp.py`) auto-allowed
every tool call whenever `APIARY_PERMISSION_MCP_URL` was unset. Two real
paths hit that: the GUI set `APIARY_PERMISSION_MCP=1` *before* starting the
loopback bridge, so a failed bind left claude spawned with
`--permission-prompt-tool` pointing at a server that approved everything;
and the mcp-config file left on disk let any `claude --mcp-config <it>`
outside the GUI get blanket approval (review C-2).

- `decide()` now denies when no bridge URL is set. Headless tests of the
  plumbing opt into auto-allow with `APIARY_PERMISSION_MCP_ALLOW_ALL=1`; the
  GUI never sets it.
- The GUI sets `APIARY_PERMISSION_MCP=1` only after `bridge.start()`
  succeeds, and pins it to `0` (TUI-banner prompts) when the bind fails.
- `permission_mcp.log` and `permission_mcp_config.json` move from
  `~/.claude/apiary_gui/` to the per-profile GUI state dir
  (`<main-apiary>/.apiary/gui/apiary_gui[_<profile>]/`). The log rotates at
  1 MiB and no longer records `Write` bodies or `Edit` strings — on the
  DECISION line as well as the REQUEST line.

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
- **The push gate no longer mistakes shell redirections for the remote.**
  `git push -q 2>&1 | tail` parsed `2>&1` as the remote name, so
  `--remotes=2>&1` matched nothing and the *entire history* was reported as
  outgoing (an already-pushed, already-allowlisted fixture blocked the
  push). Redirection tokens are skipped, and a parsed remote that isn't a
  configured one falls back to scanning against every remote instead of
  against nothing. Also from the adversarial pass: `cd sub && git push`
  is scanned in `sub` (composed with `-C`), every push in a compound
  command is scanned against its own remote (only the first used to be),
  and `--delete` / `:ref` deletions no longer count as outgoing. From the
  final review: commands are also split on newlines; `git.exe`, `/usr/bin/git`
  and `--exec-path` are recognised; a push the gate sees but cannot parse
  falls back to scanning HEAD instead of being waved through; a `cd` the
  gate cannot resolve (`cd $(...)`, `cd -`, `popd`) blocks with an
  explanation instead of scanning a guessed directory; a URL / path
  destination is scanned against the tips `git ls-remote` reports there
  (everything reachable if it cannot be reached) instead of against the
  configured remotes; and findings from one push in a compound command are
  never discarded because a later one hit a git error.
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
