# gui — apiary native desktop wrapper

Spec: scribe note `C-2026-32`. Decision: `D-2026-47` (stdlib-only deviation).

A PyWebView + PyInstaller desktop app that wraps Claude Code as a hidden pty subprocess and presents:

- a clean chat window (filtered to user-authored prompts and assistant text only)
- per-message timestamps and per-message + cumulative token counts
- a global scribe sidebar across all registered apiary repos (read-only in V1)
- a hot-reloadable theme via `<main-apiary>/.apiary/gui/apiary_gui/theme.json`
- a small pty output strip for interactive Claude Code UI (plan-mode banners, legacy permission prompts when the MCP path is off)
- a structured permission-prompt banner wired through a local MCP server (opt-in via `permission_mcp: true` in `launch.json`, or the `APIARY_PERMISSION_MCP=1` env var for a one-off) — see scribe `C-2026-36`. The server fails closed: without the GUI's bridge it denies every request, so a stale `permission_mcp_config.json` can't grant blanket approval

Windows V1 only (pywinpty). Code stays portability-clean (`pathlib`, `os.devnull`, list-form subprocess) so a V2 cross-platform port is a small delta.

## Run from source

```bash
poetry install --with gui
poetry run python -m gui.app
```

### Working on the GUI from inside the GUI

The GUI is single-instance per *profile* (Windows named mutex), so a "dev"
source build can run alongside the main packaged build by setting a profile
name. The profile re-roots all state (`tabs.json`, `sidebar_state.json`,
`theme.json`, `launch.json`, `captures/`) so the two instances don't fight:

```bash
APIARY_GUI_PROFILE=dev poetry run python -m gui.app
```

State for the dev profile lives at `<main-apiary>/.apiary/gui/apiary_gui_dev/`.
The window title becomes `apiary [dev]` so it's visually distinct from the main one.

### Frontend hot-reload

`Ctrl+R` (or `F5`) inside the window reloads `index.html` / `app.css` /
`app.js` without restarting the Python backend — open tabs, ptys, and
sidebar state survive. Useful for iterating on `gui/web/*`. Backend changes
(`gui/*.py`) still need a full process restart.

### Frontend tests

The browser-free parts of the frontend live in their own modules, each with a
`node:test` suite beside it — `prompt_detector.js` (prompt parsing),
`bubble_monitor.js` (thinking-bubble anomaly classification),
`message_reconcile.js` (optimistic-render reconciliation + queued ordering),
`thinking_state.js` (the per-tab turn state machine). `app.js` keeps the DOM
and calls into them.

```bash
node gui/web/test_thinking_state.js      # one suite
poetry run pytest gui/test_js_suites.py  # all of them, via pytest
```

`gui/test_js_suites.py` shells out to `node` for every `gui/web/test_*.js`, so
`pytest gui` covers them too. It skips rather than fails when `node` is not on
PATH. New pure logic pulled out of `app.js` should arrive with a suite.

## Build .exe (one-folder, Windows)

V1 builds a one-folder bundle for local iteration — `dist/apiary-gui/apiary-gui.exe`
plus its `_internal/` sibling. Not intended for distribution yet.

```bash
poetry install --with gui --with build
poetry run python gui/packaging/build.py
```

PyInstaller is pinned in the optional `build` poetry group
(`pyproject.toml`), so it is lockfile-managed like every other dependency
rather than `pip install`-ed ad hoc. The build script refuses to run without
it and tells you the command above.

The build script wraps `pyinstaller gui/packaging/apiary_gui.spec`, cleans
stale `build/` and `dist/apiary-gui/` first, and prints the exe path on
success. The spec bundles `gui/web/` (HTML/CSS/JS + xterm vendor) under
`_internal/gui/web/` so `Path(__file__).parent / "web"` resolves the same
way frozen as it does from source.

HiDPI: `gui/packaging/apiary_gui.manifest` declares PerMonitorV2 awareness
so the window doesn't blur on display scaling > 100%.

### Which commit is this build?

The spec stamps the commit it was built from into
`_internal/gui/build_info.json` (`{version, commit, dirty, built_at}`), and
the build script prints it:

```
built: …\dist\apiary-gui\apiary-gui.exe
stamped: 0.1.0 @ 1a2b3c4d5e6f
```

`gui/build_info.py` reads that stamp back at runtime. From source there is no
bundle, so the same answer is resolved live from git. The version shows up in
two places: the line the GUI prints to stderr on startup, and the `serverInfo`
version in the permission-MCP handshake (so `permission_mcp.log` records which
build answered).

`dirty: true` means the build came from a tree with uncommitted changes to
tracked files — the commit alone does not describe it. A build with no git
available reports `0.1.0+unknown` rather than guessing.

## State and config files

Everything lives under `<main-apiary>/.apiary/gui/apiary_gui[_<profile>]/`,
resolved by `gui/paths.py`. Auto-created on first run:

- `theme.json` — CSS variable values (hot-reloads via watchdog)
- `launch.json` — Claude Code spawn config: `command`, `args`, `cwd`, `rows`, `cols`, `permission_mcp` (defaults in `gui/theme.py::DEFAULT_LAUNCH`; unknown keys are dropped on load)

Written as you use it: `tabs.json`, `sidebar_state.json`, `composer_state.json`,
`file_refs/<session_id>.json`, `pasted/<session_id>/`, `captures/`,
`permission_mcp_config.json`, `permission_mcp.log`. Each one is described in
[File Storage](../docs/reference/file-storage.md#gui-data), and `launch.json`'s
schema in [Config Files](../docs/reference/config-files.md) — both generated or
tested against the code, so this README does not repeat them.

The list of repos whose scribe notes the sidebar can aggregate is *not* a GUI
config file — it comes from `<main-apiary>/.repos/registry.json`, the same
registry `apiary install` writes. There is no GUI-only filter file.

## Capturing pty output for new prompt handlers

When Claude Code shows an interactive UI we haven't parsed yet (plan-mode
variant, MCP OAuth, etc.), capture the raw pty stream so it can be added as
a fixture to `gui/web/test_prompt_detector.js`. Permission prompts should
now come through the structured MCP path (`APIARY_PERMISSION_MCP=1`), not
the TUI scraper — only capture them here if reproducing a scraper-mode bug.

```bash
# Launch GUI with capture on (writes <main-apiary>/.apiary/gui/apiary_gui/captures/<ts>-<label>.bin)
poetry run python -m gui.capture_session --label tool_permission

# Reproduce the UI in the GUI, then close the window.

# List existing captures
poetry run python -m gui.capture_session list

# Print one capture's tail with ANSI stripped
poetry run python -m gui.capture_session show <path-to-.bin> --tail 4000
```

Capture is controlled by the `APIARY_GUI_CAPTURE_LABEL` env var — the CLI is
just a convenience wrapper that sets it and delegates to `gui.app`. Files are
raw bytes (pre-decode) so ANSI escapes and control sequences survive intact.
