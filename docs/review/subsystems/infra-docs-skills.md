---
type: architecture
title: "Infra, docs, and skills review"
scope: project
description: Deep review of scripts, docs framework, repo hygiene, secret scanning, and all slash-command skills (2026-08-26 deep review, subsystem appendix)
framework_version: "1.0"
last_verified: 2026-08-26
---

> **Snapshot of 2026-08-26; superseded by the remediation — see CHANGELOG. Deleted at close-out (T-2026-271).**

# claude-apiary — infrastructure, docs, and skills review

Read-only review at `master` @ `1bee5e5` (2026-08-26). Every claim below was verified by reading the cited file or running the cited command; nothing was mutated.

**Headline findings (things that are broken today, not just untidy):**

1. **`/budgeter-log`, `/budgeter-warn`, `/budgeter-session-warn` do not work.** The three skill files write `~/.claude/<flag>-enabled` (`budgeter/commands/budgeter-log.md:11`, `budgeter-warn.md:11`, `budgeter-session-warn.md:11`; the installed copies in `.claude/commands/` are byte-identical) while every hook reads `<repo>/.claude/apiary/flags/<flag>-enabled` via `core/flags.py:23,64` (`budgeter/hooks/pre_tool_use.py:127,142,274`, `post_tool_use.py:53`, `stop_session.py:37`). On this machine `~/.claude/*-enabled` does not exist and `.claude/apiary/flags/` holds the real flags. The command files were last touched in the initial commit (`009c367`, 2026-03-14); `core/flags.py` changed the location on 2026-05-05 (`2149090`). SETUP.md:337-338 tells users to debug by checking the per-repo path "the slash command creates" — it does not.
2. **Claude-driven `git push` from main-apiary is currently blocked.** `poetry run python docs/check_cli_claims.py` exits 1 at HEAD (`incubator/cli.py: subcommand 'verify' exists in argparse but is undocumented`), and `core/hooks/pre_push_doc_conformer.py` blocks any Bash `git push` when that checker is nonzero. The drift was introduced yesterday (`c6ca4ea`, 2026-08-25) by documenting `verify` as its own `## incubator/cli.py verify` section (`docs/reference/cli-tools.md:292`) instead of a row in the `incubator/cli.py` Subcommands table (`:276-278`).
3. **The stalest doc in the repo is injected into every session.** `core/hooks/startup_prompt_hook.py:212-218` reads `docs/reference/cli-index.md` into startup context. That file (`last_verified: 2026-04-23`, git 2026-05-04) still lists `setup.py | --global, --project-path, --check` (`:49`) and `core/apiary_bootstrap.py` (`:50`) — both removed in the 2026-05 migration — and omits 15+ live CLIs. The hook's own instruction line (`:218`) tells Claude to run `python docs/reference/cli_lookup.py <tool>` bare, contradicting the launcher rule in `core/commands/apiary-context.md:37`.
4. **`docs/check.py` passes ("19 doc(s), all conformant") while the same 19 docs contain 60+ stale statements** (catalogued in §4). It checks frontmatter shape and whether the words "budgeter/scribe/core" appear somewhere; it cannot see 7 of the 10 tool directories.

---

## 1. Project hygiene scorecard

| Area | Status | Evidence | Recommendation |
|---|---|---|---|
| **CI** | Absent | No `.github/` directory; no workflow files anywhere (`ls .github` → not found). 516 commits, 30 PRs merged with zero automated checks. | Add one GitHub Actions workflow: matrix `ubuntu/windows/macos` × Python 3.11/3.12 running `poetry install && poetry run pytest -q` plus `python docs/check_cli_claims.py`. This is also the only way the POSIX install path (`scripts/install.sh`) will ever be exercised — the dev machine is Windows. (S) |
| **Lint** | Absent | No `ruff.toml`, `.flake8`, `setup.cfg`, `.pre-commit-config.yaml`; `pyproject.toml` has only `[tool.poetry*]` and `[tool.pytest.ini_options]` (`pyproject.toml:1-45`). A home-grown `scripts/audit_portability.py` exists but nothing references it (`git grep audit_portability` → only itself). | Add ruff with a small set (`E,F,I,PLW1514,S602,S605`) — `PLW1514` (open without encoding) and `S602` (shell=True) cover exactly what `audit_portability.py` hand-rolls. Delete the script. (S) |
| **Format** | Absent | No black/ruff-format config. Mixed quote styles across `scripts/` (`audit_portability.py` single quotes, everything else double). | `ruff format` in the same config. (S) |
| **Type checking** | Absent | No mypy/pyright config; code is well annotated (`from __future__ import annotations` in most `scripts/*.py`), so a `mypy --ignore-missing-imports core scripts` would be cheap. | Optional; add `pyright` basic mode to CI once CI exists. (S) |
| **Coverage** | Absent | No `pytest-cov`, `.coveragerc`, or `[tool.coverage]`. | Add `pytest-cov` to the dev group; report only, no gate. (S) |
| **Release / versioning** | Ceremony without motion | `VERSION` = `0.1.0`, written once in `11b6d33` (2026-05-05) and never changed (`git log -- VERSION` → 1 commit). `pyproject.toml:3` also `0.1.0`. `git tag` → empty. `CHANGELOG.md` has a single `## Unreleased` section. The pin/migration machinery — `<repo>/.claude/apiary/version.json`, `apiary doctor versions`, `migrations/v0_0_0_to_v0_1_0.py` (a no-op template, `migrations/v0_0_0_to_v0_1_0.py:86-111`) — has never had a real migration. `apiary update`, the command that would run migrations, **does not exist**: `core/cli.py:141-184` registers only `install, uninstall, self-bootstrap, doctor, mailbox, cascade-fix, version`, yet it is referenced at `CHANGELOG.md:109`, `migrations/README.md:4,38,59`, `docs/architecture/per-repo-install.md:156,241`, `core/utils/state.py:47`. | Either implement `apiary update` (~60 lines) or delete `migrations/` + `doctor versions` + the `version.json` pin and stop pretending. Tag `v0.1.0` at least once so `git describe` works. (M) |
| **Dependency locking** | Present, consistent, deprecated schema | `poetry check --lock` exits 0 (lock matches) but prints 6 warnings: `[tool.poetry.name/version/description/readme/authors]` and `[tool.poetry.scripts]` are deprecated in Poetry 2.2.1 in favour of `[project]`. `poetry lock --check` no longer exists in Poetry 2. `requirements.txt:7` (`pytest>=8.0,<9.0`) matches `pyproject.toml:26`. **`packages` list is wrong**: `pyproject.toml:7-15` includes `core, budgeter, scribe, runner, refiner, harden, gui` but omits `captures, compass, researcher, incubator, scripts, docs` — tests only pass because they run from the repo root with `--import-mode=importlib` (`pyproject.toml:44`); `pip install .` would ship a package missing four tools. | Migrate to `[project]` table; fix `packages`. (S) |
| **Secrets** | Present, two gates | Commit-time `scripts/secret_scan.py` + push-time `core/hooks/pre_push_secret_scan.py` sharing `core/secret_patterns.py`; `.secretsallow` with 4 anchored path entries. Detail in §3 — coverage is thin (misses AWS secret keys). | See §3. |
| **Line endings** | Partially pinned | `.gitattributes:5,8,9` force LF on `*.sh`, `docs/hooks/*`, `runner/hooks/*`. No `* text=auto`. `git ls-files --eol`: 234 files `i/lf w/crlf`, 120 `i/lf w/lf` — index is uniformly LF, working tree depends on each clone's `core.autocrlf`. `.ps1` uncovered (harmless). | Add `* text=auto` as line 1 so a clone with `autocrlf=false` on Windows cannot commit CRLF. (S) |
| **Ignored files** | Mostly fine, some cruft | `.gitignore:37-38` duplicate `!runner/reports/.gitkeep`. `.gitignore:62-63` (`.claude/notes.md`, `.claude/issues.md`) are made redundant by the trailing bare `.claude/` at `:68` — that line has no comment and no blank line before it because `apiary install` appended it (`core/install.py:126 _ensure_gitignore_entry`). It does not conflict with the tracked `.claude-project-key` (a root file, not under the dir). Note the inconsistency: main-apiary ignores a blanket `.claude/` but the template written into *spawned* repos uses the careful `.claude/*` + `!.claude/commands/` form (`core/install.py:348-361`). No generated artifacts tracked (`git ls-files \| grep -E 'pyc\|\.log\|dist\|build'` → empty). Untracked local junk: `.apiary.pre-migration/` (5.0 MB, ignored at `.gitignore:11`), `.apiary/screenshot.png` (220 KB), `.claude-session-identity.json` at root (2026-04-02, orphan of the removed `/startup`). | Dedupe; delete the two redundant lines; add a comment above `.claude/`; delete `.apiary.pre-migration/` and drop `.gitignore:11`. (S) |
| **Personal identifiers in git** | One | `cron_registry/DESKTOP-JO20U69.json` commits the machine hostname by design (`docs/reference/config-files.md:37`). | Acceptable for a personal repo; note it given the "sweep before push" rule. |

---

## 2. Install / update scripts

Files: `scripts/install.ps1` (402 lines), `scripts/install.sh` (139), `scripts/update.ps1` (64), `scripts/update.sh` (26), plus `scripts/preflight.py` (225) and `scripts/install_repo_hooks.py` (118) which they call.

**What's good**

- Same chain on both OSes and it matches SETUP.md:30-33: preflight → find Python → ensure Poetry → `poetry env use` → `poetry install [--with gui]` → `python -m core.cli self-bootstrap` → `scripts/install_repo_hooks.py` → `python -m core.cli doctor` (`install.ps1:352-388`, `install.sh:86-133`).
- Both drive the CLI as `python -m core.cli`, never the `apiary` console script, so the git-bash shebang trap documented at SETUP.md:88-96 can't bite (`install.ps1:19-21,380`, `install.sh:125`).
- `update.*` is genuinely one implementation: `git pull --ff-only` then re-exec the installer with the same flags (`update.ps1:53-64`, `update.sh:19-26`). Non-fast-forward aborts cleanly.
- Idempotent: `install_repo_hooks.py:69-73` upgrades an older apiary hook in place (detects `docs/check.py`) and refuses foreign hooks; `core/git_hooks.py:156-159` same policy for target repos; `hooks_dir()` honours `core.hooksPath` (`core/git_hooks.py:71-96`) — a real bug they hit and fixed (`aabfdb8`).
- `APIARY_PYTHON` override honoured identically in both (`install.ps1:194-208`, `install.sh:50-58`) and in the git hooks (`docs/hooks/pre-commit:22-23`).
- `preflight.py` is stdlib-only and imports nothing from apiary (`preflight.py:12-13`), so it really does run on a bare clone.

**Problems**

| # | Where | Issue |
|---|---|---|
| 1 | `install.ps1:56,74-79` | `-Yes` is declared and `Confirm-Or-Exit` defined but **never called** (grep: only the definition). The PowerShell installer never prompts; the bash one prompts before `pip install --user poetry` (`install.sh:98-101`). Dead code + parity gap. |
| 2 | `install.sh:102`, `install.ps1:282` | `pip install --user poetry` fails with `externally-managed-environment` (PEP 668) on Debian 12+/Ubuntu 23.04+/Homebrew Python — i.e. the exact platforms `install.sh` targets. Windows python.org builds aren't marked, so this only bites POSIX, which has never been tested (no CI, Windows dev box). Use `pipx install poetry` or the official installer with a fallback. |
| 3 | `install.sh:24-31`, `update.sh:26` | No `--dry-run` on POSIX; `update.sh` forwards `"$@"`, so `./scripts/update.sh --dry-run` exits 2 "unknown argument". SETUP.md:53-54 is honest about this ("The Windows installer takes -DryRun") but it is still a parity gap. |
| 4 | `install.ps1:356` | Preflight runs with the *uncleaned* PATH (`& $pythonExe @pfArgs`, not `Invoke-Clean`), contradicting the "every child process this script spawns gets a PATH with [WindowsApps] removed" claim at `:84`. Harmless today because preflight doesn't spawn Python. |
| 5 | `install.ps1:60,111` | `$ErrorActionPreference='Stop'` + `2>$null` on a native command: in PS 5.1 any stderr output becomes a terminating `NativeCommandError`, caught at `:114` → the interpreter is silently rejected. A Python that emits a warning on startup (e.g. `PYTHONWARNINGS` set, or a venv with a broken `sitecustomize`) is treated as "not runnable". Fragile; consider `$ErrorActionPreference='Continue'` inside the probe. |
| 6 | `install.ps1:278-281` | In `-DryRun`, `Ensure-Poetry` returns the literal string `'poetry'` when Poetry is absent, then `Invoke-Clean 'poetry' …` prints `[dry-run] poetry env use …` — the dry run reports a command that would not resolve. Cosmetic. |
| 7 | `install_repo_hooks.py:110-111` | `main()` prints `Installing repo-local git hooks into <REPO_ROOT>/.git/hooks` even when `_git_hooks_dir()` (`:32-42`) has redirected to `core.hooksPath`. The per-hook lines that follow are correct; the banner lies. |
| 8 | `install_repo_hooks.py:29` | Imports `hooks_dir` from `scripts.install_git_hooks` — a re-export shim — instead of `core.git_hooks` where it lives (`core/git_hooks.py:71`). Two-hop import through a CLI module. |
| 9 | SETUP.md vs scripts | SETUP.md:114 says `python scripts/install_repo_hooks.py`; the installers run it as `poetry run python scripts/install_repo_hooks.py` (`install.ps1:384`, `install.sh:129`). Both work (stdlib only). SETUP.md:122-127 uses `python .claude/apiary/launch.py scripts/install_git_hooks.py` (cwd-relative) rather than the PORTABILITY.md:88 idiom — fine for a human at repo root, inconsistent as documentation. |
| 10 | Three bootstrap generations coexist | `scripts/bootstrap.py` (388 lines, legacy, writes `~/.claude/auto-startup-enabled` at `:43-45,247-252`, `mkdir ~/.claude` at `:239`, offers to install context rules into `~/.claude/CLAUDE.md` at `:279-334`, hand-rolls a TOML parser at `:117-134` despite stdlib `tomllib`), `setup.py` (redirect stub), and `apiary self-bootstrap` (live). Nothing invokes `scripts/bootstrap.py` (`git grep bootstrap.py` → only docs), yet PORTABILITY.md:101,109 and `docs/reference/cli-tools.md:869`, `config-files.md:37` still describe it as live. It directly contradicts "Apiary writes nothing to `~/.claude/`" (SETUP.md:306, PORTABILITY.md:10). |

**Windows-vs-POSIX parity summary**: ps1 has py-launcher + registry discovery (`:138-184`), globs, `-DryRun`, a GUI-too-new warning (`:345-349`), and `-SkipBootstrap`; sh has name-probe + pyenv (`:59-77`), `--skip-bootstrap`, a Poetry prompt, no dry-run. The GUI-version warning on POSIX is delegated to `preflight.py --gui` (`preflight.py:68-80`), so that gap is covered. Net: acceptable, but the POSIX script has never run anywhere and has a known PEP-668 failure mode.

---

## 3. Secret scanning

Components: `scripts/secret_scan.py` (490 lines), `core/secret_patterns.py` (111, 9 rules), `core/git_hooks.py` (installer), `docs/hooks/pre-commit` (main-apiary), `docs/hooks/pre-commit-secret-scan` (targets), `.secretsallow`, 35 tests in `scripts/test_secret_scan.py`, parity tests in `core/test_secret_patterns.py`.

### Coverage — empirically

I ran `scan_lines()` over representative lines (probe script in scratchpad; nothing written to the repo):

| Sample | Result | Why |
|---|---|---|
| `aws_secret_access_key = <40-char base64 value>` | **MISS** | Generic rule's key alternation is `\b(?:…\|secret\|…\|access[-_]?key…)\b` (`secret_scan.py:109-110`); `_` is a word char, so `\b` never fires inside `aws_secret_access_key`. The single most common leaked credential on GitHub is invisible to the default scan. `--entropy` catches it, but the hooks run `--staged --quiet` without it (`docs/hooks/pre-commit:35`, `pre-commit-secret-scan:50`). |
| `AWS_SECRET_ACCESS_KEY="…"` | MISS | same |
| `password = "<value containing &>"`, `password = "<value containing @ and !>"` | MISS | Value class `[A-Za-z0-9_\-./+=]{8,}` (`:113`) stops at `&`/`@`/`!`, then `(?P=quote)` demands the closing quote immediately → no match. Passwords with punctuation — the strong ones — evade. Even `--entropy` misses them (too short). |
| `token = "<16-char literal>"  # see get_config()` | MISS | `_INDIRECTION` (`:90-104`) includes `\w+\s*\(` ("any function call") and `\{[A-Za-z_]`, searched over `line[m.start("value"):]` (`:161`) — i.e. to end of line including comments. Any trailing comment mentioning a call or a brace silences the line. |
| `my_password_value = "<literal>"` | MISS | key must be a bare word; `password` inside `my_password_value` fails `\b`. |
| `github_pat_11ABCDEFG…` (fine-grained PAT) | MISS | `github-token` rule is `gh[pousr]_…` only (`core/secret_patterns.py:66-70`). |
| `sk_live_<24 chars>` (Stripe) | MISS | OpenAI rule is `sk-` with hyphen (`:63`). |
| JWT `eyJhbGciOi…`, `npm_…`, a Django `secret_key` literal with the `django-insecure-` prefix | MISS | no rules; `secret_key` fails the same `\b` problem (`secret` followed by `_`). |
| `PASSWORD=<bare literal with a digit>`, `password="<literal>"` | HIT generic-assignment | |
| `DATABASE_URL=postgres URL with user:password@ before the host` | HIT basic-auth-url | |
| `api_key: "<AWS access key id>"`, `sk-ant-api03-…`, `ghp_…` | HIT | |

Blocked filenames (`:187-195`) are sensible (`.env*`, `id_rsa`, `*.pem/pfx/p12/keystore/jks`, `.aws/credentials`, `.npmrc`, `.pypirc`) but miss `*.key`, `*.ppk`, `.netrc`, `.git-credentials`, `.htpasswd`, `service-account*.json`, `kubeconfig`, `*.tfvars`. `SKIP_CONTENT` (`:199-204`) means any file named `*.min.js` or with a `.pdf/.zip/…` extension is never content-scanned — a naming bypass.

Coverage summary: 9 literal rules vs detect-secrets' ~25 plugins / gitleaks' 150+ rules; the generic rule has two regex defects (`\b` around underscore-joined keys; punctuation in quoted values) that defeat it on the highest-value cases.

### False-positive controls

Reasonable and well-tested: placeholder list (`:72-85`), digit-only skip, bare-word prose filter `_looks_like_a_credential` (`:133-147`), one finding per line (`:350`), lockfile/binary skips, inline pragma honouring both spellings (`core/secret_patterns.py:96-98`), `.secretsallow` regexes. The indirection filter is tuned too far toward silence (see above).

### Bypasses

- `.secretsallow` is read from the **working tree** (`load_allowlist(root)` → `root / ".secretsallow"`, `:300-307`), not the index. An unstaged local edit adding `.` silences everything; and every regex is tested against the *line* as well as the path (`:324`), so a loose path regex (e.g. `test`) exempts any line containing that word repo-wide. Acceptable for a single-user repo; worth a comment.
- `git commit --no-verify` (documented at `:35-36`, SETUP.md:120). Pre-commit does not run for `git cherry-pick`, `git rebase`, `git merge`, or `git commit -n`; the push gate only fires for Claude-driven pushes (`core/hooks/pre_push_secret_scan.py` is a PreToolUse hook), so a terminal `git push` of a cherry-picked commit is unscanned by both gates.
- `git diff --cached` is run without `--no-ext-diff`/`--no-textconv`/`-c core.quotepath=false` (`:399`); a quoted path (`+++ "b/…"`, non-ASCII dir) keeps its quote, `b/` stripping fails (`:283`), and the blocked-file regexes anchored on `$` no longer match.
- Excerpt "redaction" is not redaction: `_redact()` (`:224-232`) truncates at 100 chars and the docstring says "never the tail", but every matched credential in this repo's rule set is < 100 chars, so the hook prints the full secret to stderr. The push gate's doc claims values are redacted (`docs/reference/hooks.md:46`); the commit gate's output contradicts that.

### "Fails closed" (CHANGELOG.md:50-53)

- **True at the reachability layer.** `docs/hooks/pre-commit-secret-scan:33-48` exits 1 with instructions when the launcher or main-apiary is missing; `:22-31` exits 1 when no Python 3 is found. Main-apiary's `docs/hooks/pre-commit:34-35` uses `|| STATUS=1` for both checks and keeps running so all failures are reported.
- **False inside the scanner.** `_git()` (`secret_scan.py:243-259`) returns `""` on *any* failure (OSError, nonzero exit). `scan_staged()` (`:397-402`) then sees an empty path list and an empty diff → zero findings → exit 0 → commit allowed. Triggers: `git` missing from the hook's PATH, an `index.lock` left behind, a corrupted index, git printing to stdout with nonzero exit. Only the `repo_root() is None` case is handled (`:469-472`, exit 2). Fix: make `_git` raise (or return `None`) and exit 2 in `--staged` mode.
- The per-repo hook runs the scanner with the hook's probed interpreter (`py -3`/`python3`), not main-apiary's venv — fine because the scanner is stdlib-only; but it also means a target repo's commit succeeds or fails based on whether *main-apiary's checkout* is intact, which is by design and documented.

### Hand-rolled vs gitleaks / detect-secrets (PORTABILITY.md:90, CHANGELOG.md:47-49)

The stated argument — "those need a per-machine binary install, so a fresh clone on a new OS would silently skip the check" — is:

- **Valid for gitleaks/trufflehog** (Go binaries, not pip-installable in a portable way).
- **Not valid for detect-secrets**, which is pure Python and could be a dev dependency next to pytest in `pyproject.toml:26`. The repo already requires Poetry and a venv; "per-machine binary" is not the real constraint.
- **The real constraint is unstated**: git hooks resolve `py -3`/`python3`/`python` (`docs/hooks/pre-commit:21-26`), *not* the Poetry venv, so anything the hook imports must be stdlib. That is a good reason and it should be the one written down.
- **"Silently skip" is a design choice, not an inherent property** — the same hook already fails closed when main-apiary is unreachable; it could equally fail closed when `gitleaks` is absent.
- PORTABILITY.md:90 also promises "An external scanner may be used as optional escalation when it's already on PATH" — nothing in `secret_scan.py` implements that (`grep -n which secret_scan.py` → none).

Verdict: hand-rolling is defensible given venv-less hook execution, but the implementation is a fraction of what the borrowed rule sets provide and has two regex defects on the highest-value cases. Best path: keep the stdlib gate, vendor gitleaks' regex table (MIT) into `core/secret_patterns.py`, fix the generic rule, enable entropy on generic-assignment values only, make `_git` fail closed, and implement the promised optional `gitleaks protect --staged` escalation.

Tests: 35 tests with a positive case per pattern (`test_secret_scan.py:44-59`), pragma/allowlist/dotenv/diff-parsing coverage, but no negative-coverage tests that would have exposed the misses above.

---

## 4. Docs framework

### What it actually checks

`docs/check.py` (288 lines), run by `docs/hooks/pre-commit:34` on every commit:

- Frontmatter present; six required keys; `type ∈ {reference, architecture, standard, guide}`; `scope ∈ {budgeter, scribe, core, project, docs}` (`check.py:39-41`); `last_verified` matches `\d{4}-\d{2}-\d{2}` (`:113-115`) — **format only, never compared with the file's git date**; `framework_version` equals `_framework.md`'s `version` — which has been `"1.0"` since 2026-04-02 (`docs/_framework.md:210`) and never bumped, so that warning has never fired.
- "Coverage": whether the strings `budgeter`, `scribe`, `core` appear anywhere in the concatenated reference docs (`:125-138`) — vacuous.
- Hook/command/CLI coverage limited to `KNOWN_TOOLS = {"budgeter","scribe","core"}` (+ `docs`) (`:44,157,187,208`). **Invisible**: harden, refiner, compass, researcher, runner, incubator, captures, gui, scripts. Every doc for those tools must declare `scope: project` to pass.
- Index completeness (`:227-241`) — useful, tiny.

Result today: `Framework v1.0 — 19 doc(s), all conformant`. It has never caught a content drift and cannot by construction.

`docs/check_cli_claims.py` (353 lines) is the part that pulls its weight: it shells out to 43 tools' `--help` and reconciles subcommand/flag names both ways. It fixed 34 real drifts once (`e6e5538`, T-2026-237) and correctly reports the live one today. Weaknesses: (a) it is **not** in the pre-commit hook — only `check.py` is (`docs/hooks/pre-commit:34-35`) — so drift lands in commits and only trips at push via `core/hooks/pre_push_doc_conformer.py`, where the fix is a doc edit; (b) `SKIP_HEADERS` (`:42-52`) excludes `apiary` (`core/cli.py`), the most user-facing CLI, so `cli-tools.md:943` can omit `doctor stale` (`core/doctor.py:156`) unnoticed; (c) the doc's section-per-subcommand pattern (`## incubator/cli.py verify`, `cli-tools.md:292`) fights the checker's model and is both the cause of the current drift and reported as "skipped".

`docs/hooks/remind_standards.py`: registered per-repo (`core/hooks_factory.py:136`). `known_dirs = {"budgeter","scribe","core","docs",".claude"}` (`:72`) → editing `runner/executor.py`, `harden/lenses.py`, `gui/app.py`, etc. is classified as a **new tool** and nudges you to read `new-tool-checklist.md` — a doc that tells you to register hooks in `setup.py`. Unused `import json` (`:10`).

`docs/reference/cli_lookup.py`: fine. `--help` returns "No tool matching '--help'" exit 1.

### Drift the framework did not catch — exhaustive list

Legend: **G** = references the removed global `~/.claude` install; **S** = references `setup.py` as live; **M** = references deleted `MIGRATION-PLAN.md`; **X** = references a removed module/script/command; **C** = contradicts another doc or the code.

**README.md** (git 2026-06-08)
- `:22` "the only third-party dependency is pytest" — `pyproject.toml:33-41` gui group (pywebview, pythonnet, pywinpty, watchdog); README:186 itself says `poetry install --with gui`. **C**
- `:62` "Handoffs — structured session summaries generated automatically on startup from the previous session's transcript" — handoffs are written by `/wrapup` (`core/commands/wrapup.md:31-36`); `docs/architecture/system-overview.md:88` says so. **C**
- `:71` `/startup` — no `*/commands/startup.md` exists; startup is `core/hooks/startup_prompt_hook.py`. **X**
- `:81` "Set `APIARY_STATE_LAYOUT=legacy` … to read the pre-migration `~/.claude/projects/<project-key>/` path" — removed (`CHANGELOG.md:79-80`); only a comment survives at `scribe/notes.py:47`. **X G**
- `:176` "canonical state lives in `runner/cron_registry.json`" — it is `cron_registry/<hostname>.json` (`docs/reference/config-files.md:37`, `runner/cron_health.py`); no such file is tracked. **X**
- `:223-338` Repository Structure: `core/flags.py # (~/.claude/{name}-enabled)` (`:227`, vs `core/flags.py:4-5`) **G**; `core/hooks/` lists 3 of 13 hooks (`:230-232`); missing `core/{cli,install,doctor,drift,mailbox,cascade,git_hooks,secret_patterns,self_bootstrap,uninstall,targets,context_rules,apiary_profiles,launcher_template,sanitizer,transcript}.py`; missing `budgeter/{tune,log_agent_cost,query_request}.py`, `budgeter/lib/query.py`; missing `harden/validate_common.py`; `runner/cron_registry.json` (`:320`), `runner/backlog/` (`:323`), `runner/intake/` (`:324`) do not exist (`ls runner/backlog runner/intake` → no such file; artifacts live at `<state-dir>/runner/` per `runner/target_repo.py:128-138`) **X**; missing `runner/{usher,usher_order,run_history,run_lock,run_tracker,git_lib,abort,monolithic_executor,target_repo,schema_versions,close_source_todo}.py`, `runner/hooks/post-merge`; `setup.py # Unified installer for all tools` (`:336`) **S**; entire top-level dirs absent: `gui/`, `captures/`, `incubator/`, `scripts/`, `docs/`, `profiles/`, `migrations/`, `context-rules/`, `cron_registry/`, and `PORTABILITY.md`, `CHANGELOG.md`, `VERSION`, `pyproject.toml`.
- `:380-384` Testing: only `python budgeter/test_hooks.py`; there are 98 test files and the runner is `poetry run pytest` (`pyproject.toml:43-44`).

**SETUP.md** (git 2026-08-25)
- `:16` "see `MIGRATION-PLAN.md` for the full design" — deleted in `f1220d8`. **M**
- `:165` "Toggles persist per-repo at `<repo>/.claude/apiary/flags/`" and `:337-338` "The slash command creates/removes this file" — the slash commands write `~/.claude/` (headline #1). **C**
- `:219-220` "`apiary doctor versions` flags it. The versioned migration runner under `migrations/` chains the upgrade scripts" — no runner/`apiary update` exists (`core/cli.py:141-184`). **X**
- `:306` "Apiary writes nothing to `~/.claude/`" — contradicted by `budgeter/commands/*.md:11`, `scripts/bootstrap.py:239-252`, `scripts/install_context_rules.py:44`, `scripts/uninstall_hooks.py:37`. **C G**

**PORTABILITY.md** (git 2026-08-25)
- `:9` "`cmd.exe` and PowerShell are not supported" — SETUP.md:34-38 ships `install.ps1`; `core/commands/apiary-context.md:15` says the idiom "works identically in Bash and PowerShell". **C**
- `:10` "writes nothing to `~/.claude/`" — see above. **C G**
- `:23-24` pip alternative then `poetry run apiary self-bootstrap` — the pip path registers no `apiary` script; only `python -m core.cli` works. **C**
- `:72` "apiary_bootstrap provenance record" — module removed (`CHANGELOG.md:84`). **X**
- `:79-80` "The full canonical list lives in the user's global `CLAUDE.md`" — points at a private file outside the repo.
- `:90` "An external scanner may be used as optional escalation" — not implemented.
- `:95` "(T5a–T5d)" — opaque ticket IDs.
- `:101,109-110` `python scripts/bootstrap.py` troubleshooting — legacy script, not in the install chain. **X**

**docs/reference/cli-index.md** (last_verified 2026-04-23; **injected into every session** by `core/hooks/startup_prompt_hook.py:214`)
- `:49` `setup.py | Unified installer | --global, --project-path, --check` **S**
- `:50` `core/apiary_bootstrap.py` **X**
- `:16` `scribe/notes.py` lists 9 of 21 subcommands.
- Missing entirely: `captures/cli.py`, `core/targets.py`, `core/doctor.py`, `harden/lenses.py`, `harden/validate_consolidation.py`, `runner/mark_done`, `scripts/{secret_scan,install_git_hooks,install_repo_hooks,preflight,install_context_rules}.py`, `docs/check*.py`, `incubator verify`, `apiary`.

**docs/reference/cli-tools.md** (last_verified 2026-06-08)
- `:12` "No external dependencies — stdlib only" while documenting `gui/app.py` (`:882-901`). **C**
- `:121-122` doctor "Phase-0 scaffold; `--fix` actions land in later phases. See `MIGRATION-PLAN.md` §7.6" — `--fix` exists (`per-repo-install.md:236-245`). **M C**
- `:292` `## incubator/cli.py verify` — the section shape that trips `check_cli_claims.py` (headline #2).
- `:542,569` `install_context_rules.py` "into `~/.claude/CLAUDE.md`" **G**
- `:869` "`scripts/bootstrap.py` invokes `check` at the tail of its run" **X**
- `:896-897` GUI state at `~/.claude/apiary_gui_<profile>/`, `~/.claude/apiary_gui/captures/` — moved to `<main-apiary>/.apiary/gui/` (`file-storage.md:90-99`, `CHANGELOG.md:113-115`). **G C**
- `:943` `apiary doctor` subcommands omit `stale` (`core/doctor.py:156`).
- `:1044-1055` "All tests use `unittest` and are run directly" — `budgeter/test_hooks.py` is not unittest; `python -m runner.test_orchestrator` only works from the root.

**docs/reference/slash-commands.md** (last_verified 2026-04-21)
- `:12` "loads them at session start from `~/.claude/commands/`" — per-repo `<repo>/.claude/commands/` (`file-storage.md:172`). **G**
- `:37-43` toggles at `~/.claude/<name>-enabled` — matches the broken commands, contradicts `core/flags.py:23`. **G C**

**docs/reference/hooks.md** (last_verified 2026-06-09)
- `:12` "registered in `~/.claude/settings.json`" vs `:84` (same file) "`<repo>/.claude/settings.json`". **G C**
- `:20` "Stop — When the session ends" vs `:62` "Stop hooks fire at the end of every assistant turn (not session end)". **C**
- `:51` "(managed by `setup.py`)" **S**; `:53-56` lists 4 PreToolUse hooks; the real `.claude/settings.json` has 15 PreToolUse entries (`per_repo_drift_check`, `check_install`, `inject_session`, `startup_hook`, `learnings_inject_hook`×3, `research_capture_reminder`, `pre_push_doc_conformer`, `pre_push_secret_scan`, `budgeter/pre_tool_use`×4, `remind_standards`).

**docs/reference/config-files.md** (git 2026-08-25)
- `:135` "`~/.claude/settings.json`. Managed by `setup.py`" **G S**
- `:153-155` `.claude-session-identity.json` "Written by `/startup`" — no such command; identity now at `<main-apiary>/.repos/<slug>/sessions/identity-<sid>.json` (`per-repo-install.md:106`). **X C**
- `:173-177` `.secretsallow` example shows 2 of the 4 real entries (minor).

**docs/reference/file-storage.md** (last_verified 2026-05-04)
- `:15` "User-global state under `~/.claude/` — shared across all repos (transcripts, GUI per-instance state, flags)" **G**
- `:22-33` "All runner artifacts are repo-local under `runner/`… git-ignored except `runner/intake/`" — `runner/target_repo.py:128-138` resolves `<state-dir>/runner/`; PORTABILITY.md:71 agrees. **C**
- `:41-42` flag files at `~/.claude/` **G**
- `:67` "falls back to `<git-repo-root>/.apiary/scribe/` … `APIARY_STATE_LAYOUT=legacy`" **X**
- `:138` "`core/apiary_bootstrap.py` writes its provenance record" **X**
- `:157-158` transcripts at `~/.claude/.last-transcript.jsonl`, `~/.claude/transcripts/` — `per-repo-install.md:107` says `<main-apiary>/.repos/<slug>/sessions/transcripts/`; `CHANGELOG.md:72-73` says `~/.claude/transcripts/` was deleted. **G C**

**docs/architecture/system-overview.md** (last_verified 2026-04-07)
- `:36,76-78` `/startup` **X**
- `:43-49` tool table has 5 rows; compass, researcher, captures, runner, incubator, gui absent.
- `:94` `core/flags.py` "sentinel files at `~/.claude/{name}-enabled`" **G**

**docs/architecture/per-repo-install.md** (last_verified 2026-05-06)
- `:56` `.apiary/legacy/orphan-*.json` "phase-3 migration leftovers" — no such dir (`ls .apiary` → forwarding, gui, pointer, runner, scribe, screenshot.png). **X**
- `:156,241` `apiary update` **X**
- `:236-245` doctor table lacks `stale`.

**docs/standards/code-style.md** (last_verified 2026-04-07)
- `:17` "Stdlib only — hard rule" — gui group is a documented exception elsewhere (`cli-tools.md:884`, D-2026-47) but not here. **C**
- `:72` "Use `unittest` (stdlib). No pytest." — see §8; the canonical runner *is* pytest. **C**
- `:82` "e.g. `--global | --project-path | --check`" **S**

**docs/standards/new-tool-checklist.md** (git 2026-05-05 — touched by the migration commit and still says)
- `:5,40,59,77` register/warn in `setup.py` **S**; `:45` "Copied to `~/.claude/commands/` by `setup.py`" **G S**; `:60` "copies commands and agents to `~/.claude/`" **G S**; `:76` "append rules to `~/.claude/CLAUDE.md`" **G**.

**docs/guides/adding-a-hook.md** — `:5` "registering it in setup.py", `:53-58` "### 2. Register in setup.py" vs its own checklist `:76` "Added to the appropriate builder in `core/hooks_factory.py`". **S C**
**docs/guides/adding-a-command.md** — `:66` "Added to `setup.py` copy list" vs `:46-48` (install.py). **S C**
**docs/guides/adding-a-tool.md** — `:68` "`~/.claude/CLAUDE.md`", `:69` "setup.py warning". **G S**
**docs/guides/bootstrapping-a-repo.md** — `:128` "Loads `.apiary/bootstrap_state.json`" vs `:45` (same doc) `<main-apiary>/.repos/<name>-<uid>/bootstrap_state.json` **C**; `:153` link to `cli-tools.md#coreapiary_bootstrappy` — dead anchor. **X**
**docs/commands/review.md** — `:73` "setup.py integration complete". **S**
**docs/_index.md** — `:3` "Last updated: 2026-04-02".
**migrations/README.md** — `:6,78` MIGRATION-PLAN **M**; `:4,38,59` `apiary update` **X**; `:80-85` "This directory is being created during phase 0" (present tense, 4 months later); `:12-16` table lists `v0_1_0_to_v0_2_0.py`, `v0_3_5_to_v0_3_6.py` that do not exist.
**CHANGELOG.md** — `:100` "per `MIGRATION-PLAN.md` §3.10" **M**; `:109` `apiary update` **X**; `:124-125` "`scripts/phase3_*.py` … `scripts/phase5_cleanup_global.py`" — deleted in `8d45c65`. **X**
**Code comments** — `setup.py:5`, `pyproject.toml:22`, `scripts/install_repo_hooks.py:11`, `core/flags.py:15`, `core/utils/state.py:47`: MIGRATION-PLAN refs **M**; `scribe/notes.py:47` APIARY_STATE_LAYOUT **X**; `budgeter/test_hooks.py:753` checks `~/.claude/budgeter-log-enabled` **G**.
**runner/cron_setup.md** — a 5-line "superseded by scheduling.md" stub still tracked.

Count: 60+ distinct stale statements across 20 files; ~25 mention `~/.claude` as live state; 12 name `setup.py` as the installer; 10 point at the deleted `MIGRATION-PLAN.md`.

### Is the framework worth its weight?

- `docs/check.py` + frontmatter + `_framework.md` (210 lines of templates): **ceremony**. Cost: every doc carries 6 frontmatter keys that are never validated for truth; a `_framework.md` versioning scheme that has never bumped; a pre-commit run on every commit. Value delivered: index completeness and "frontmatter exists". It gave 4 months of green checks while the docs rotted through a migration.
- `docs/check_cli_claims.py` + pre-push conformer: **worth keeping**, but move it to pre-commit (it's ~10 s) and drop the separate-section pattern for subcommands.
- `remind_standards.py`: net negative today because its classification is stale; fix or delete.

---

## 5. Migration leftovers

| Item | Status | Action |
|---|---|---|
| `scripts/phase3_*.py`, `scripts/phase5_cleanup_global.py` | Already deleted (`8d45c65` "cleanup: remove one-shot migration scripts post per-repo cutover"). Only `CHANGELOG.md:124-125` still says they exist. | Reword CHANGELOG to past tense or drop the paragraph. |
| `MIGRATION-PLAN.md` | Deleted in `f1220d8` (phase 6). 10 dangling refs: SETUP.md:16, CHANGELOG.md:100, migrations/README.md:6,78, cli-tools.md:122, setup.py:5, pyproject.toml:22, scripts/install_repo_hooks.py:11, core/flags.py:15, core/utils/state.py:47. | Remove refs; if §-numbers matter, resurrect the plan under `docs/architecture/` — but `per-repo-install.md` already covers it. |
| `.apiary.pre-migration/` | Untracked, ignored (`.gitignore:11`), 5.0 MB, last write 2026-05-04: compass observations, hooks, observer, research, and a `pointer` with an absolute path. | Delete (or zip outside the repo); drop `.gitignore:11`. |
| `.apiary/screenshot.png` | 220 KB untracked junk (2026-06-06). | Delete. |
| `.claude-session-identity.json` (root) | 2026-04-02, orphan of the removed `/startup`; ignored at `.gitignore:59`. `config-files.md:153-155` still documents it as written by `/startup`. | Delete file + doc section + gitignore line. |
| `setup.py` redirect stub | 4 months post-migration; sole purpose is muscle memory (SETUP.md:347-348). Side effect: any tool that sees a `setup.py` assumes setuptools. | Delete; keep the one-line note in SETUP.md Troubleshooting. |
| `scripts/bootstrap.py` + `scripts/test_bootstrap.py` (388 + 243 lines) | Legacy pre-per-repo bootstrap; writes `~/.claude/auto-startup-enabled` (`:43-45,247-252`); nothing invokes it; superseded by `apiary self-bootstrap`. Still documented as live in PORTABILITY.md:101,109, cli-tools.md:869, config-files.md:37. | Delete both + the doc refs. (Its cron-health tail call, `:367-384`, is the only unique behaviour — move that into `apiary doctor` if wanted.) |
| `scripts/uninstall_hooks.py` + test (177 + 268) | Targets `~/.claude/settings.json` global hooks (`:37`), which CHANGELOG.md:70-75 says were deleted. `apiary uninstall` covers per-repo. | Delete both; drop the mentions in `core/hooks_lib.py:5,74,213`. |
| `scripts/install_context_rules.py` + test (542 + 396) | Targets `~/.claude/CLAUDE.md` (`:44`); the per-repo zone is now written by `core/install.py:311-323`. Still referenced by `core/hooks/startup_hook.py:99` (drift line) — that hook also still reads `~/.claude/CLAUDE.md` (`startup_hook.py:74-99`) for a zone apiary no longer writes. `STOPGAP_MARKERS = ()` (`:52-57`) and `--replace-stopgap` (`:475`) operate on an empty tuple. | Either delete script + test + the `~/.claude/CLAUDE.md` branch of `startup_hook.py`, or rename it explicitly as the *global* opt-in and stop claiming apiary writes nothing to `~/.claude`. Recommend delete. |
| `scripts/audit_portability.py` | Unreferenced (`git grep` → only itself). `ALLOW` hardcodes 4 files (`:15-20`). | Replace with ruff `PLW1514`,`S602`; delete. |
| `scripts/retrotag_learnings.py` | "One-shot retrofit" (`:2`) still wired into `scribe/commands/review-learnings.md:57` as a bare `python scripts/…` call. | Keep the capability, fold into `scribe/notes.py retrotag`, fix the skill's invocation. |
| `migrations/` (`README.md` + no-op `v0_0_0_to_v0_1_0.py`) | Documents an `apiary update` runner that was never built; VERSION has never moved. | Build `apiary update` (small) or delete the directory and the `version.json` pin path. |
| `runner/cron_setup.md` | Superseded stub (`:3`). README.md:319 still lists `scheduling.md (supersedes cron_setup.md)`. | Delete. |
| `profiles/apiary.jsonc` | "identical to `base`" (`profiles/apiary.jsonc:1-4`); `core/cli.py:145` defaults to `base`. | Keep (cheap), or delete until it diverges. |
| `.apiary/legacy/orphan-*.json` | Documented at `per-repo-install.md:56`; directory does not exist. | Remove the doc line. |

---

## 6. Skills as a set

20 tracked prompt files: 16 slash commands (`*/commands/*.md`, 92,369 bytes ≈ 23k tokens) + 4 harden agent templates (`harden/agents/*.md`, 11,890 bytes ≈ 3k tokens). `.claude/commands/` holds 16 untracked byte-identical copies installed by `apiary install` (`core/install.py:299-308` lists the 10 source dirs). Tokens ≈ bytes/4.

| Skill (file) | Lines | ~Tokens | Frontmatter | Launcher idiom | CLI-flag accuracy | Notes / overlap |
|---|---|---|---|---|---|---|
| `/apiary-context` (`core/commands/apiary-context.md`) | 104 | 1,580 | yes | ✔ (9 calls) | ✔ | Not a procedure — a context dump. Three layers load the same thing: this skill, the CLAUDE.md zone rule (`context-rules/behavioral/load_apiary_context.md`) that merely points at it, and `startup_prompt_hook.py`. |
| `/wrapup` (`core/commands/wrapup.md`) | 113 | 1,380 | yes | ✔ (5) | ✔ (`learn --content --session-id`, `add --type handoff --summary`) | Step 4 (`:55-113`, half the file) is a prose reimplementation of observation extraction that `compass/backfill.py` does in Python. Should be a `compass/capture.py` CLI. |
| `/budgeter-log` (`budgeter/commands/budgeter-log.md`) | 14 | 150 | **none** | ✗ writes `~/.claude/budgeter-log-enabled` (`:11`) | n/a | **Broken** (headline #1). `core/flags.py` has `toggle()` (`:95`) but no `__main__`; needs a CLI. |
| `/budgeter-warn` | 14 | 180 | **none** | ✗ (`:11`) | n/a | Broken; identical to above modulo one word. |
| `/budgeter-session-warn` | 14 | 260 | **none** | ✗ (`:11`) | n/a | Broken; third copy. Collapse all three into one `/budgeter <flag>`. |
| `/budgeter-setup` (`budgeter/commands/budgeter-setup.md`) | 35 | 330 | **none** | n/a (`poetry run apiary install` from main-apiary, `:22`) — correct: no launcher exists pre-bootstrap | ✔ | Name is misleading: it bootstraps apiary, not budgeter. Overlaps SETUP.md step 3. |
| `/note` (`scribe/commands/note.md`) | 33 | 280 | yes | ✔ (2) | ✔ | `/note done <N>` (`:24-27`) is a "close" verb hiding inside the "add" command. |
| `/notes` (`scribe/commands/notes.md`) | 35 | 330 | yes | ✔ (1) | **✗** `:21` `/notes learning → notes.py list --type learning` — `list --type` choices are `{todo,handoff,decision,wishlist,reference,blocker,context,general}` (verified via `--help`); `learning` is rejected. Should map to `notes.py learnings`. | |
| `/review-learnings` (`scribe/commands/review-learnings.md`) | 74 | 950 | yes | **partial** — `:57` bare `python scripts/retrotag_learnings.py`; `:63` bare `python -c "from scribe.notes import …"` (needs main-apiary on `sys.path`; the claim at `:66` "works regardless of cwd" is false outside main-apiary) | ✔ (`archive-learning`, `supersede --content`) | |
| `/refine` (`refiner/commands/refine.md`) | 249 | 3,180 | yes | ✔ (7) | ✔ | `:225` "Re-run the 8 validation rules" vs `:202` "9 validation rules" — internal drift. |
| `/harden` (`harden/commands/harden.md`) | **746** | **9,300** | yes | ✔ (22) | ✔ (all 12 invocations checked against `--help`: `round_counter defender --set/--get`, `validate_and_assign findings --lens --sanitize --check-files --deep`, `consolidation --source-ids --degrade`, `response --expected-ids`, `lenses list/codes/json`, `query_request --request-id --cwd`) | A program written in prose: 3 execution paths, a cost formula (`:189-199`), retry/degrade logic, budget abort, worktree lifecycle, temp-file size check (`:116-137`). `runner/auto_harden.py` already does this loop in Python. 40% of all skill tokens. |
| `/review` (`docs/commands/review.md`) | 106 | 810 | yes | ✔ (2) | ✔ | `:73` "setup.py integration complete". Overlaps the built-in `/code-review`, `/simplify`, `/security-review`; its unique value is running `docs/check.py` + loading `docs/standards/*`. |
| `/compass-sync` (`compass/commands/compass-sync.md`) | 48 | 590 | yes | ✔ (4) | ✔ | Relies on `launch.py core/utils/state.py` printing the state dir as a side effect (`:40`) — undocumented CLI. Same trick in `wrapup.md:105`, `apiary-context.md:54`. |
| `/research` (`researcher/commands/research.md`) | 79 | 930 | yes | ✔ (6) | ✔ (`add <topic> "<title>" --tags`, `find`, `list --topic`, `show`, `verify`, `register-tag`) | Clean; near-duplicate of `cli-tools.md:332-355` — the skill *is* the CLI doc. |
| `/runner-prep` (`runner/commands/runner-prep.md`) | 160 | 1,520 | yes | **partial** — `:98` `python -m runner.validate_intake` (cwd-dependent) | **path wrong** — `:68,71,98,114` write/validate `runner/intake/<uuid>.json`; intake lives at `<state-dir>/runner/intake/` (`runner/target_repo.py:147`). Also has Claude hand-author the JSON instead of calling `runner/create_intake.py --from-todo` (`cli-tools.md:621-640`). | |
| `/incubator` (`incubator/commands/incubator.md`) | 120 | 1,310 | yes | ✔ (3) | ✔ (`spawn --path --spec-note-id --session-id`, `verify --path`) | Good example of the right shape: thin orchestration + mandatory verify step. |
| `harden/agents/attacker.md` | 47 | 600 | prompt | n/a | — | Legacy single-attacker; category vocab `general/security/input/logic/complexity/resilience` differs from the 7-lens set. |
| `harden/agents/attacker_lens.md` | 50 | 690 | prompt | n/a | — | fine |
| `harden/agents/consolidator.md` | 67 | 840 | prompt | n/a | — | fine |
| `harden/agents/defender.md` | 73 | 850 | prompt | n/a | — | `:6` "(with ATK-NNN IDs)" — multi-lens feeds `CON-NNN` (`harden.md` 2e says the Defender is prefix-agnostic). Minor. |

**Naming consistency**: file stem = `name:` for the 12 that have frontmatter; the 4 budgeter files have no frontmatter at all and rely on the first `#` heading (Claude Code tolerates it) — they violate the template in `docs/guides/adding-a-command.md:26-31`.

**Structure consistency**: two conventions — `# /name — Title` + `## Arguments` + `## Step N` (refine, harden, incubator, runner-prep, research) vs bare `## Steps` (wrapup, note, notes, review-learnings, compass-sync, budgeter-*) vs `### N.` (review). Pick one.

**Launcher compliance**: 11/16 fully compliant; 3 broken (`budgeter-*`), 2 partial (`review-learnings`, `runner-prep`). `budgeter-setup` is legitimately exempt.

**Too long**: `harden.md` (746) and `refine.md` (249) — together 54% of skill tokens; both are procedures the LLM must *execute faithfully* from prose. `wrapup.md` Step 4 and `runner-prep.md` Step 4 are the same anti-pattern in miniature (LLM hand-writes JSON that a CLI should produce).

**Duplicates**: budgeter-log/warn/session-warn (3→1); `/research` ≈ `cli-tools.md` researcher section; `/apiary-context` ≈ startup hook payload.

---

## 7. Code quality — scripts/ and docs/*.py

Worst offenders, in order:

1. **`scripts/bootstrap.py`** — 388 lines of dead legacy: hand-rolled TOML scanner (`:117-134`) in a Python ≥ 3.11 codebase that has `tomllib`; global `~/.claude` writes (`:43-45,239,247-252`); `import subprocess` inside a function (`:329`); `_check_requirements` "verifies each dependency imports" by `__import__(name.replace("-","_"))` (`:172-177`) which is wrong for any package whose import name differs from its dist name. Nothing calls it.
2. **`scripts/secret_scan.py`** — `_git()` swallows every failure into `""` (`:243-259`) → fail-open (§3); `_redact` (`:224-232`) does not redact; `_matches(path)` (`:327`) is a name that says nothing; `Pattern` (`:119-130`, frozen dataclass) re-wraps `core.secret_patterns.SecretPattern` (NamedTuple) at `:175-177` purely so `_GenericAssignPattern` can override `search` — one class with an optional predicate would do; regex defects at `:109-113` (§3). The `# noqa: null-device` comments at `:280-283` exist to appease `audit_portability.py`, which nothing runs.
3. **`scripts/install_git_hooks.py`** — `:37-54` re-exports 11 names from `core.git_hooks` including the private alias `_classify`, and `core/git_hooks.py:114-116` keeps `_classify = classify` "because the CLI and its tests referenced the private name". Tests (`scripts/test_install_git_hooks.py`) should import `core.git_hooks`; the shim and alias should go.
4. **`scripts/install_repo_hooks.py`** — `:29` two-hop import through the shim; `:110-111` misleading banner when `core.hooksPath` is set; `:23-29` constants interleaved with `sys.path` hacking + `# noqa: E402` for a linter that isn't configured.
5. **`scripts/install_context_rules.py`** — 542 lines with an empty `STOPGAP_MARKERS` tuple and a 5-line comment about why it's empty (`:52-57`), plus a `--replace-stopgap` flag (`:475`) that iterates that empty tuple. Superseded by `core/install.py`.
6. **`docs/check.py`** — hardcoded `KNOWN_TOOLS`/`VALID_SCOPES` (`:39-44`) blind to 7 tool dirs; `check_coverage` (`:125-138`) is a substring grep for three words; `os.walk`/`os.path` (`:73-77`) in a repo whose `code-style.md:65` mandates pathlib; `main()` returns `None` on the empty path and `sys.exit`s elsewhere (`:255-282`).
7. **`docs/hooks/remind_standards.py`** — `known_dirs` (`:72`) misclassifies 7 tools as "new tool"; unused `import json` (`:10`).
8. **`scripts/audit_portability.py`** — unreferenced; `NULL_RE` (`:23`) flags the word "nul" in prose; allow-list by filename (`:15-20`).
9. **`scripts/install.ps1`** — dead `-Yes`/`Confirm-Or-Exit` (`:56,74-79`); otherwise well-commented and careful.
10. **`docs/check_cli_claims.py`** — good. Nits: 43+ sequential subprocesses with no parallelism (~10-15 s); `SKIP_HEADERS` (`:42-52`) hides the `apiary` console script from reconciliation; `is_tool_section` (`:301-305`) means any `## path.py extra` header is silently "skipped" rather than flagged as malformed.
11. **`scripts/preflight.py`** — clean. Nit: base-install minimum reuses `GUI_PY_MIN` (`:50`), coupling the floor to a GUI constant.
12. **`docs/reference/cli_lookup.py`** — fine; `--help` → "No tool matching" exit 1.

---

## 8. Tests

`poetry run pytest scripts docs -q` → **143 passed in 14.62s** (scripts: 126 tests across 6 files; docs: 17 in `docs/test_check_cli_claims.py`).

Repo-wide: **98 test files, 22,072 lines**. Style split:

- **97/98 unittest-style** (`unittest.TestCase` + `unittest.main()`; e.g. `docs/test_check_cli_claims.py:11`).
- **1/98 custom harness**: `budgeter/test_hooks.py` — bare `test_*(tmp_path)` functions (`:78-494`) with pytest-fixture-shaped signatures, plus its own `main()` (`:751-…`) that passes a `TemporaryDirectory` when run as a script. It also still checks `~/.claude/budgeter-log-enabled` (`:753`). Under pytest the `tmp_path` fixture is injected; under `python budgeter/test_hooks.py` the harness runs. So the file is *both* — and README.md:383 / cli-tools.md:1044 document only the script form.
- **0/98 import pytest** (`grep -lE "^import pytest|pytest\.(mark|fixture|raises)"` → none). The `code-style.md:72` rule "Use unittest. No pytest." is followed in letter; but the canonical runner is pytest (`pyproject.toml:43-44`, the dev dependency at `:26`, the instructions in memory). Reword the rule to "unittest-style test classes, executed by pytest; no pytest-only APIs."
- `scripts/test_bootstrap.py:49` patches `CLAUDE_DIR` so the legacy bootstrap tests don't touch the real home — good, and moot once the script is deleted.

---

## 9. Verdicts

| Item | Verdict | Reason |
|---|---|---|
| `scripts/install.ps1` | keep / improve | Solid; remove dead `-Yes`, clean-PATH the preflight call. |
| `scripts/install.sh` | improve | PEP-668 `pip install --user` failure; add `--dry-run`; never exercised — needs CI. |
| `scripts/update.ps1`, `update.sh` | keep | Correct thin wrappers. |
| `scripts/preflight.py` | keep | Clean, stdlib, honest. |
| `scripts/install_repo_hooks.py` | improve | Import from `core.git_hooks`; fix banner. |
| `scripts/install_git_hooks.py` | improve | Drop the 11-name re-export shim and `_classify`. |
| `scripts/secret_scan.py` + `core/secret_patterns.py` | improve (rules) / rewrite (generic rule) | Fail-open `_git`, `\b` defect, punctuation defect, missing AWS-secret/PAT/Stripe rules, no real redaction. |
| `docs/hooks/pre-commit`, `pre-commit-secret-scan` | keep / improve | Add `check_cli_claims.py` to main-apiary's hook. |
| `scripts/bootstrap.py` + test | **delete** | Legacy, unreferenced, writes `~/.claude`. |
| `scripts/uninstall_hooks.py` + test | **delete** | Targets deleted global install. |
| `scripts/install_context_rules.py` + test | **delete** (with `startup_hook.py`'s `~/.claude/CLAUDE.md` branch) | Superseded by per-repo zone. |
| `scripts/audit_portability.py` | **delete** | Replace with ruff rules. |
| `scripts/retrotag_learnings.py` | improve | Fold into `notes.py retrotag`; fix skill invocation. |
| `setup.py` | **delete** | Redirect stub past its usefulness. |
| `migrations/` | delete or finish | No runner exists; VERSION never moved. |
| `.apiary.pre-migration/`, `.apiary/screenshot.png`, root `.claude-session-identity.json` | **delete** | Local cruft. |
| `runner/cron_setup.md` | **delete** | Superseded stub. |
| `docs/check.py` + `_framework.md` frontmatter scheme | rewrite (shrink) | Keep index check + frontmatter presence; derive tool list from the tree; add `last_verified` vs git-date staleness; drop the version ceremony. |
| `docs/check_cli_claims.py` | keep / improve | Move to pre-commit; reconcile `apiary` too; flag malformed headers. |
| `docs/hooks/remind_standards.py` | improve or delete | Fix `known_dirs` or remove. |
| `docs/reference/cli_lookup.py` | keep | |
| `docs/reference/cli-index.md` | **rewrite** | Injected every session; stalest doc in the repo. Generate it from `check_cli_claims`' introspection. |
| `docs/reference/{slash-commands,hooks,file-storage,config-files}.md` | improve | 20+ `~/.claude`/`setup.py` statements. |
| `docs/architecture/system-overview.md` | rewrite | Pre-migration, 5 of 11 tools. |
| `docs/standards/new-tool-checklist.md`, `guides/adding-*.md` | improve | `setup.py` → `core/install.py` + `hooks_factory.py`. |
| `README.md` Repository Structure | **delete the section** (or generate it) | 40% wrong; will drift again. |
| `README.md` rest | improve | `/startup`, handoffs, `APIARY_STATE_LAYOUT`, cron registry, Testing. |
| `SETUP.md`, `PORTABILITY.md` | improve | Mostly accurate; fix the listed lines. |
| `CHANGELOG.md` | improve | phase scripts, `apiary update`, MIGRATION-PLAN. |
| `budgeter/commands/budgeter-{log,warn,session-warn}.md` | **rewrite** | Functionally broken; merge into one. |
| `scribe/commands/notes.md` | improve | `learning` mapping. |
| `scribe/commands/review-learnings.md` | improve | Two bare `python` invocations. |
| `runner/commands/runner-prep.md` | improve | Wrong intake path; use `create_intake.py`. |
| `harden/commands/harden.md` | rewrite (extract to Python) | 746 lines of prose orchestration. |
| `refiner/commands/refine.md` | keep / trim | 8-vs-9 rules nit. |
| `core/commands/wrapup.md` | improve | Move compass capture to a CLI. |
| Remaining skills (`apiary-context`, `note`, `research`, `compass-sync`, `incubator`, `review`) | keep | |
| `profiles/`, `context-rules/`, `cron_registry/` | keep | Small and live. |
| `pyproject.toml` | improve | `[project]` table; fix `packages`. |
| `.gitignore`, `.gitattributes` | improve | Dedupe; `* text=auto`. |

---

## 10. Top 10 recommended changes (value ÷ effort)

1. **Fix the budgeter toggles** (S). Add a `__main__`/argparse to `core/flags.py` (`toggle <name>` printing ON/OFF), point one merged `/budgeter <log|warn|session-warn>` skill at it via the launcher, delete the three `~/.claude` one-liners, fix SETUP.md:337-338 and `slash-commands.md:37-43`. A user-facing feature has been silently dead since 2026-05-05.
2. **Unblock pushes and tighten the conformer loop** (S). Move `verify` into the `incubator/cli.py` Subcommands table (`cli-tools.md:276-278`), delete the `## incubator/cli.py verify` section; add `check_cli_claims.py` to `docs/hooks/pre-commit`; remove `apiary` from `SKIP_HEADERS` and document `doctor stale`.
3. **Regenerate `cli-index.md` from introspection** (S/M). It is the one doc every session reads (`startup_prompt_hook.py:214`); derive the table from `check_cli_claims.introspect()` so it cannot drift, and fix the bare `python docs/reference/cli_lookup.py` instruction at `:218` to the launcher idiom.
4. **Delete the migration corpse** (S). `setup.py`, `scripts/bootstrap.py`+test, `scripts/uninstall_hooks.py`+test, `scripts/install_context_rules.py`+test (and `startup_hook.py`'s `~/.claude/CLAUDE.md` branch), `scripts/audit_portability.py`, `runner/cron_setup.md`, `.apiary.pre-migration/`, `.apiary/screenshot.png`, root `.claude-session-identity.json`; scrub the 10 `MIGRATION-PLAN.md` refs and CHANGELOG.md:124-125. ~2,700 lines of dead code and tests gone; "writes nothing to `~/.claude/`" becomes true.
5. **Sweep the 60 stale doc statements** (M). Mechanical: `~/.claude` → per-repo paths; `setup.py` → `core/install.py`/`hooks_factory.py`; `/startup` → startup hook; remove `APIARY_STATE_LAYOUT`, `apiary_bootstrap`, `apiary update`; fix runner artifact location in `file-storage.md`; reconcile `hooks.md` Stop semantics and the 4-vs-15 hook order. Replace README's Repository Structure with a 10-line "top-level map" or drop it. Bump `last_verified` only on the docs actually re-read.
6. **Harden the secret scanner** (M). Fix `_GENERIC_ASSIGN` (`(?<![A-Za-z0-9])` instead of `\b`, allow `[A-Za-z0-9_]*` key prefixes, widen the quoted-value class to `[^"'\s]{8,}`); scope `_INDIRECTION` to the value token, not the rest of the line; add rules for AWS secret keys (`(?i)aws.{0,20}secret.{0,20}['"=:\s][A-Za-z0-9/+=]{40}`), `github_pat_`, `sk_live_/rk_live_`, `npm_`, `pypi-AgEI`, Slack webhook URLs, JWT; enable entropy on generic-assignment values only; make `_git` failure exit 2 in `--staged` mode; actually redact (`head[:6] + "…" + len`). Add the misses above as regression tests. Optionally implement the promised `gitleaks` escalation.
7. **Add CI** (S). One workflow, three OS × two Python, `poetry install`, `poetry run pytest -q`, `python docs/check_cli_claims.py`, `python scripts/secret_scan.py --path .`. This is the only way `install.sh` and the POSIX git-hook path get exercised.
8. **Add ruff + fix packaging** (S). `ruff` (`E,F,I,PLW1514,S602`) replaces `audit_portability.py`; migrate `pyproject.toml` to `[project]`; add the missing `packages` (`captures, compass, researcher, incubator, scripts`); add `* text=auto`; dedupe `.gitignore`.
9. **Extract `/harden` (and `/wrapup` Step 4) orchestration into Python** (L). A `harden/orchestrate.py` that owns path selection, size check, cost estimate, request-id, worktree, validation/retry/degrade, budget abort, TODO filing, and summary — the skill shrinks to "confirm config → call orchestrator → spawn agents when it asks → show result" (~100 lines). `runner/auto_harden.py` is 80% of this already. Same pattern turns wrapup's 60-line JSON-authoring prose into `compass/capture.py`. Saves ~10k tokens per invocation and removes the largest source of LLM-execution variance in the repo.
10. **Decide the versioning story** (M). Either implement `apiary update` (`core/cli.py` subcommand + a 40-line chain runner over `migrations/`, bump `VERSION` to `0.2.0`, tag) or delete `migrations/`, `doctor versions`, and the `version.json` pin, and say so in `per-repo-install.md`. Today the docs describe a mechanism that does not exist and the mechanism that does exist has never run.

Smaller fixes worth bundling with the above: `notes.md:21` (`learning` → `learnings`), `runner-prep.md` intake path + use `create_intake.py --from-todo`, `review-learnings.md:57,63` bare `python` calls, `remind_standards.py:72` `known_dirs`, `refine.md:225` "8" → "9", `defender.md:6` ID prefix wording, `install.ps1` dead `-Yes`, `install.sh` PEP-668 fallback, `docs/check.py` `KNOWN_TOOLS` derived from `*/commands` + `*/hooks`.
