# gui — apiary native desktop wrapper

Spec: scribe note `C-2026-32`. Decision: `D-2026-47` (stdlib-only deviation).

A PyWebView + PyInstaller desktop app that wraps Claude Code as a hidden pty subprocess and presents:

- a clean chat window (filtered to user-authored prompts and assistant text only)
- per-message timestamps and per-message + cumulative token counts
- a global scribe sidebar across all registered apiary repos (read-only in V1)
- a hot-reloadable theme via `~/.claude/apiary_gui/theme.json`
- a small pty output strip for interactive Claude Code UI (permission prompts, plan-mode banners)

Windows V1 only (pywinpty). Code stays portability-clean (`pathlib`, `os.devnull`, list-form subprocess) so a V2 cross-platform port is a small delta.

## Run from source

```bash
poetry install --with gui
poetry run python -m gui.app
```

## Build .exe (one-folder, Windows)

V1 builds a one-folder bundle for local iteration — `dist/apiary-gui/apiary-gui.exe`
plus its `_internal/` sibling. Not intended for distribution yet.

```bash
poetry run pip install "pyinstaller>=6.0,<7.0"   # one-time, build-only dep
poetry run python gui/packaging/build.py
```

The build script wraps `pyinstaller gui/packaging/apiary_gui.spec`, cleans
stale `build/` and `dist/apiary-gui/` first, and prints the exe path on
success. The spec bundles `gui/web/` (HTML/CSS/JS + xterm vendor) under
`_internal/gui/web/` so `Path(__file__).parent / "web"` resolves the same
way frozen as it does from source.

HiDPI: `gui/packaging/apiary_gui.manifest` declares PerMonitorV2 awareness
so the window doesn't blur on display scaling > 100%.

## Config files

Auto-created on first run under `~/.claude/apiary_gui/`:

- `theme.json` — CSS variable values (hot-reloads)
- `launch.json` — Claude Code spawn args + cwd
- `apiary_repos.json` — list of apiary repos to aggregate scribe notes from
- `captures/` — raw pty-output captures (only populated when capture mode is on)

## Capturing pty output for new prompt handlers

When Claude Code shows an interactive UI we haven't parsed yet (permission
prompt variant, plan-mode, MCP OAuth, etc.), capture the raw pty stream so it
can be added as a fixture to `gui/test_prompt_detector.py`.

```bash
# Launch GUI with capture on (writes ~/.claude/apiary_gui/captures/<ts>-<label>.bin)
poetry run python -m gui.capture_session --label tool_permission

# Reproduce the UI in the GUI, then close the window.

# List existing captures
poetry run python -m gui.capture_session list
```

Capture is controlled by the `APIARY_GUI_CAPTURE_LABEL` env var — the CLI is
just a convenience wrapper that sets it and delegates to `gui.app`. Files are
raw bytes (pre-decode) so ANSI escapes and control sequences survive intact.
