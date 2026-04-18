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
pip install -r gui/requirements.txt
python -m gui.app
```

## Build .exe

```bash
pip install pyinstaller
pyinstaller gui/packaging/apiary_gui.spec
```

## Config files

Auto-created on first run under `~/.claude/apiary_gui/`:

- `theme.json` — CSS variable values (hot-reloads)
- `launch.json` — Claude Code spawn args + cwd
- `apiary_repos.json` — list of apiary repos to aggregate scribe notes from
