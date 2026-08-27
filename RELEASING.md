# Releasing

Apiary is not published to PyPI and nobody installs it from a tarball. A
"release" here means one thing: **`<main-apiary>/VERSION` moves, every
bootstrapped repo is migrated to match, and the commit that did it carries a
git tag** so a repo that has been dormant for months can be told exactly which
migration chain it missed.

## What the version means

`VERSION` is a single line of 3-part semver. It describes the **per-repo
install layout** — the files `apiary install` writes into a target repo and
the shape of the state under `<main-apiary>/.repos/`. It is not a product
version and has nothing to say about how good the tools are.

Bump it when a bootstrapped repo needs something done to it that a plain
re-install would not do. Everything else — a new slash command, a bug fix, a
doc rewrite — ships without a bump, and `apiary doctor stale` is what catches
repos running old command files.

| Change | Bump | Migration file |
|---|---|---|
| New/renamed/moved file under `<repo>/.claude/apiary/` | minor | yes |
| Schema change to a pin file, `bootstrap_state.json`, or `registry.json` | minor | yes |
| State moved between directories under `.repos/<slug>/` | minor | yes |
| Data fix-up in already-bootstrapped repos (bad timestamps, stray files) | patch | yes |
| New slash command, hook, tool, or doc | none | no |

## Cutting a release

1. **Write the migration** if the change needs one. `migrations/v<from>_to_v<to>.py`,
   full 3-part semver with dots as underscores; the contract (idempotent,
   atomic, forward-only, no external side effects) is in
   [`migrations/README.md`](migrations/README.md). Migrations stay in git
   forever — a v0.1 repo coming back online in a year chains through all of
   them.
2. **Bump `VERSION`** to the new number, in the same commit or PR as the
   migration.
3. **Update `CHANGELOG.md`** — the human-readable half of the same story.
4. **Run the checks**: `poetry run pytest -q`, `poetry run python docs/check.py`,
   `poetry run python docs/check_cli_claims.py`,
   `poetry run python scripts/secret_scan.py --staged`.
5. **Merge to `master`.**
6. **Migrate the fleet**:

   ```bash
   poetry run apiary version --all      # who is on what; `!` marks drift
   poetry run apiary update --dry-run   # which migrations would run
   poetry run apiary update             # run them and re-pin
   poetry run apiary doctor versions    # should be clean afterwards
   ```

   `update` walks each repo's chain in order and rewrites its pin after every
   step, so an interrupted run resumes where it stopped. A migration that
   raises stops that repo only; the others still get updated and the command
   exits 1.
7. **Tag the merge commit**, annotated, on `master`:

   ```bash
   git checkout master
   git pull
   git tag -a v0.1.0 -m "apiary v0.1.0 — per-repo install layout"
   git push origin v0.1.0
   ```

   Tags are `v` + the contents of `VERSION`. `v0.1.0` is the current layout
   and has never been tagged; tag it from the commit that `VERSION` last
   changed in (`git log --oneline -- VERSION`) rather than from HEAD, so the
   tag names the layout it describes.

## Rolling back

There is no `downgrade()`. Rolling a repo back to an older layout means
checking out the older main-apiary and re-running `apiary install --target`,
which rewrites every generated file from that checkout. Data written by a
migration stays written — which is why migrations are required to be
idempotent and to avoid destructive edits.
