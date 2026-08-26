---
type: architecture
title: "GUI subsystem review"
scope: project
description: Deep review of gui/: PyWebView desktop wrapper, pty, transcript tail, permission MCP (2026-08-26 deep review, subsystem appendix)
framework_version: "1.0"
last_verified: 2026-08-26
---

# Code review: `gui/` (claude-apiary)

Scope: every non-test `.py` in `gui/` (25 files), `gui/web/{app,prompt_detector,bubble_monitor,file_drop}.js`, `index.html`, `gui/packaging/*`, `gui/README.md`, the GUI section of `README.md`, `docs/standards/code-style.md`, all 17 Python test files (skimmed) and both Node test files. Read-only; tests executed via `poetry run pytest gui -q` (9 runs) and `node gui/web/test_*.js`. Third-party internals consulted where a claim depended on them: `.venv/Lib/site-packages/winpty/ptyprocess.py` (pywinpty 2.0.15) and `.venv/Lib/site-packages/webview/util.py` (pywebview 5.4).

All line references are to the working tree at commit `1bee5e5`.

---

## 1. What it is

A Windows-only desktop wrapper around the Claude Code CLI, ~15.5k lines (44 `.py`, 3.3k-line `app.js`, 2.2k-line `app.css`).

**Process model.** `gui/app.py:1035 main()` acquires a per-profile Win32 named mutex (`single_instance.py:20`, `paths.py:91`), creates one PyWebView/WebView2 window with `js_api=GuiBridge` (`app.py:1088-1096`), and on the `loaded` event calls `App.start_services()` (`app.py:852`). The same exe doubles as an MCP stdio server when invoked with `--mcp-server` (`app.py:1041-1043`, `permission_mcp.py:41`).

**Per tab** an `App` holds a `Session` (`session.py:115`) which owns: a `PtyWrapper` spawning `claude` under pywinpty (`pty_wrapper.py:164-170`) with a `pty-reader` thread (`:174-177`); a `SessionDiscovery` thread polling `~/.claude/projects/<key>/` every 2s for the newest JSONL (`transcript.py:332-413`, `session.py:190-194`); a `TranscriptTail` thread polling that JSONL every 100ms (`transcript.py:195`, `session.py:371-376`); a `ScribeAggregatorService` thread every 5s (`scribe_aggregator.py:201`); a `SubagentTracker` thread every 2s (`subagent_tracker.py:85`). That is five threads per tab. Globally: a watchdog `Observer` + debounce `Timer` for `theme.json` (`theme.py:164-167`), a `usage-poller` thread hitting a private OAuth usage endpoint every 60s (`usage_fetcher.py:89`), an optional `ThreadingHTTPServer` on loopback for MCP permission round-trips (`permission_bridge.py:56-65`), and one thread per JS→Python bridge call (pywebview `util.py:234-236`).

**Bridge.** Python→JS is fire-and-forget string evaluation: `App._eval` serialises `window.evaluate_js("window.apiary.onX(<json>)")` behind `_js_lock` (`app.py:397-406`); 13 `on*` entry points live in `app.js:2859-3082`. JS→Python is `pywebview.api.<method>` on `GuiBridge` — 30 public methods (`app.py:66-348`).

**Source of truth.** The chat view is rendered exclusively from the session JSONL (`transcript.py:1-18`); the pty stream is only shown in an xterm.js pane and scraped for interactive prompts (`app.js:1557`, `2490`). Three prompt channels share one banner: xterm scrape (`app.js:2490 runDetect`), MCP permission bridge (`app.js:2250`), and transcript-sourced AskUserQuestion (`ask_prompt.py`, `app.js:2083`).

**State files.** Per-profile under `<main-apiary>/.apiary/gui/apiary_gui[_<profile>]/` (`paths.py:81-88`): `theme.json`, `launch.json` (`theme.py:22-24`), `tabs.json` (`tabs_state.py:30-31`), `sidebar_state.json`, `composer_state.json`, `file_refs/<sid>.json` + `pasted/<sid>/` (`file_refs.py:57-60`), `captures/` (`pty_capture.py:28`). **Outside** the profile dir: `~/.claude/apiary_gui/permission_mcp.log` and `permission_mcp_config.json` (`permission_mcp.py:43-44`), `~/.claude/apiary_gui/bubble_anomalies.jsonl` (`app.py:63`). Reads `~/.claude/.credentials.json` (`usage_fetcher.py:45`) and `<main-apiary>/.repos/registry.json` (`repo_registry.py:33`). Browser `localStorage` holds five UI prefs (`app.js:1055-1127`, `2544`).

**Packaging.** `gui/packaging/build.py` wraps `pyinstaller gui/packaging/apiary_gui.spec` into a one-folder bundle `dist/apiary-gui/apiary-gui.exe` (`build.py:75-111`); the spec bundles `gui/web/` under `_internal/gui/web/` (`apiary_gui.spec:28-32`), declares PerMonitorV2 DPI (`apiary_gui.manifest:10-11`), `console=False` (`spec:68`).

---

## 2. Architecture assessment

### Backend/frontend split — sound in principle, leaky in practice

The split puts the right things in Python: session lifecycle, transcript parsing, file references (made Python-authoritative at send time, `file_refs.py:233-270`, `app.py:194-204`), permission decisions. The frontend is a pure renderer for those. That part is good.

What leaks:

- **UI-state ownership is split three ways.** Tabs and collapsed groups persist via Python (`tabs_state.py`, `sidebar_state.py`); composer/quick-capture drafts and turn state live only in a JS `Map` (`app.js:1685-1716`) and die on `Ctrl+R`; usage/sidebar/flash/agents-dismissed prefs live in `localStorage` (`app.js:1055-1127`, `2544-2554`). Nothing decides which mechanism a new preference should use.
- **Three prompt paths, one banner, hand-rolled mutual exclusion.** `runDetect` yields to `askPromptId` (`app.js:2495`); `onPermissionPrompt` tears down a scraped prompt (`:3044`); `onAskPrompt` refuses if `mcpPrompt` is set (`:3061`); `setActiveSession` nulls `mcpPrompt` directly (`:2896`). Each path has its own state slot (`activePrompt`, `mcpPrompt`, `askPromptId`, `awaitingFeedback`, `:1950-1965`) and its own teardown (`hidePromptBanner :2328`, `resolveMcpPrompt :2301`). This is where the next inconsistency bug will come from.
- **No protocol layer.** Every push is an f-string (`app.py:412-507`); every handler re-parses `typeof x === "string" ? JSON.parse(x) : x` (`app.js:2916`, `2928`, `2968`, `2986`, `2997`, `3021`, `3039`, `3058`). A single `push(event, payload, sid)` / `dispatch(event)` pair would remove ~150 lines and give one place to log/validate.

### `app.js` is a monolith and should be split

3,293 lines in one IIFE covering at least 14 concerns: tab bar (`:36-106`), folder picker (`:108-309`), markdown/table renderer (`:429-517`), scroll (`:519-549`), token totals (`:561-599`), message render + optimistic reconciliation (`:606-815`), sidebar notes (`:844-1034`, `1394-1432`), usage meters (`:1036-1392`), quick capture (`:1433-1525`), xterm + resize (`:1533-1676`), per-tab state + thinking bubble + anomaly monitor (`:1678-1922`), prompt detection/banners/arrow driver (`:1924-2531`), agents strip (`:2533-2802`), bridge surface (`:2804-3082`), composer/paste/drag-resize (`:3099-3292`). Twenty-plus module-level mutable `let`s are shared across those sections, and five `setInterval`s run permanently (`:1392`, `1842`, `1883`, `2530`, `2787`). The repo already proved the alternative works — `prompt_detector.js`, `bubble_monitor.js`, `file_drop.js` are self-contained IIFEs with Node tests. The pattern should be applied to the rest.

### Threading model — pushes are safe, App state is not

- Python→JS is correctly serialised (`app.py:401`). But `evaluate_js` is synchronous (pywebview docstring: "Javascript code is evaluated synchronously"), so **every pty chunk is a blocking round-trip on the pty reader thread** (`app.py:433-436` ← `pty_wrapper.py:208`). A stalled JS main thread back-pressures the ConPTY output pipe and stalls `claude` itself. Not a bug today, but it couples UI responsiveness to the CLI's stdout.
- **App state has no lock.** pywebview runs each `pywebview.api.*` call on its own thread (`webview/util.py:234-236`: "executed in a separate thread"). `open_session`/`switch_to`/`close_session` mutate `_sessions`/`_active_idx` (`app.py:752-753`, `766`, `824-830`); `active` is an index dereference (`:389-393`) read by every other bridge method, by `_on_file_drop` on a pywebview worker (`:601`), and indirectly by the HTTP handler thread that mutates `_pending_permission_by_session` (`:473-479`) while `_sweep_stale_pending_permissions` (`:668-684`) and `resolve_permission` (`:549-554`) mutate the same dicts from other threads. See Bugs #5/#6.
- `SubagentTracker._scan_once` holds an `RLock` for the whole scan including disk reads (`subagent_tracker.py:151-238`), so `note_parent_record` on the transcript-tail thread blocks behind file I/O every 2s (`:279`). Harmless at current scale; a design smell.
- Theme watcher, usage poller, aggregator and discovery threads are all stop-event driven and joined with timeouts (`theme.py:172-183`, `usage_fetcher.py:120-125`, `scribe_aggregator.py:234-237`, `transcript.py:227-230`, `384-387`) — good. `start_services` is idempotent across reloads (`app.py:858-861`) — good; that was clearly learned the hard way.

### JSONL tailing — reasonable, but blind to format drift

`filter_record` (`transcript.py:89-152`) is defensive (every field type-checked, unknown shapes dropped) so format drift fails *quiet* rather than loud — the safe direction. But:

- User records are kept only when `message.content` is a **string** (`transcript.py:109`). Claude Code writes list-form content for user turns that include images/pasted blocks; those prompts vanish from the chat. The GUI's own paste-image path sidesteps this by shipping file paths as text (`file_refs.py:263-267`), so it only bites when the user types in the terminal pane.
- There is no version marker or "unknown record type" counter surfaced to the UI; `on_skip` exists (`transcript.py:208-214`, `:247`, `:269`) but no caller ever passes it (`session.py:371-376`).
- `SessionDiscovery` pins by `st_ctime` (`transcript.py:306`, `376`), which is creation time only on Windows — see Bug #8.

### Coupling to Claude Code internals — extensive and smeared

Twelve distinct undocumented-internal dependencies across nine files:

| Internal | Where |
|---|---|
| Transcript record shape (`type`, `isMeta`, `promptId`, `attachment`, `stop_reason`, usage keys, `<local-command-caveat>`/`<command-name>`/`<task-notification>` prefixes) | `transcript.py:96-141` |
| cwd → project-key transform (`\ / . space` → `-`) | `session.py:40-75` |
| Env vars that make a nested claude auto-approve | `session.py:101-107` |
| Bracketed-paste + CR submit timing | `session.py:96-97`, `419-456`, `pty_wrapper.py:112-143` |
| CLI flags `--permission-mode`, `--mcp-config`, `--permission-prompt-tool` | `session.py:274-285` |
| `subagents/agent-*.jsonl` + `.meta.json` sidecar (`agentType`, `description`) | `subagent_tracker.py:167`, `372-380` |
| AskUserQuestion tool input schema | `ask_prompt.py:41-59`, `app.js:2083-2118` |
| Private `/api/oauth/usage` endpoint + `~/.claude/.credentials.json` layout + beta header | `usage_fetcher.py:43-58` |
| MCP protocol `2024-11-05` + the three methods claude calls | `permission_mcp.py:34`, `237-247` |
| TUI glyphs (`❯`, `>` since 2.1.116), nav-footer phrasing, box-drawing chrome | `prompt_detector.js:31-47`, `72` |
| Feedback-option wording ("tell…what to change", "type something", "chat about") | `app.js:2411-2412` |
| `/model` and `/clear` slash commands; ESC×4 + Ctrl+U interrupt cadence | `app.js:412`, `session.py:88`, `app.js:2830-2840` |

None of this is isolated behind a single "claude-code adapter" boundary. The cleanest sub-pieces (`ask_prompt.py`, `transcript.filter_record`, `_claude_code_project_key`) are at least pure functions with tests; the rest is inline.

### Build reproducibility — inputs pinned, process not

Runtime deps are lock-pinned (pywebview 5.4, pywinpty 2.0.15, watchdog 6.0.0, pythonnet 3.0.5 — `poetry.lock:355-456`). But PyInstaller is installed ad hoc with `poetry run pip install "pyinstaller>=6.0,<7.0"` (`gui/README.md:50`, not in `pyproject.toml`), xterm 5.5.0 is vendored from jsDelivr with a header that says "Do NOT use SRI with dynamically generated files" (`gui/web/vendor/xterm/xterm.min.js:1`) and no recorded hash, the manifest version is a fixed `1.0.0.0` (`apiary_gui.manifest:6`), `SERVER_VERSION = "0.1.0"` (`permission_mcp.py:36`), and no CI ever builds it. Two builds from the same commit on two machines are not guaranteed identical, and nothing stamps which commit a given exe came from.

---

## 3. Bugs and correctness risks (ordered by severity)

**#1 HIGH — Permission MCP server fails open when the bridge is absent.**
`permission_mcp.decide` auto-allows every tool call if `APIARY_PERMISSION_MCP_URL` is unset (`permission_mcp.py:204-206`, `172-173`). Two concrete paths hit that:
(a) `App._start_permission_bridge` sets `os.environ["APIARY_PERMISSION_MCP"]="1"` (`app.py:517`) *before* `bridge.start()`; if the loopback bind raises `OSError` it prints and returns (`:519-523`) leaving the flag on and the URL env unset. `Session._start_pty` then still passes `--permission-prompt-tool` (`session.py:279-285`), and the MCP server approves everything silently. The only trace is a stderr line that is invisible in a `console=False` build (`apiary_gui.spec:68`).
(b) `write_mcp_config` leaves `~/.claude/apiary_gui/permission_mcp_config.json` on disk after exit, rewritten on every spawn (`session.py:281`); any `claude --mcp-config <that file> --permission-prompt-tool mcp__apiary__permission_prompt` launched outside the GUI gets blanket auto-approval. Requires the opt-in flag, but "opt-in to a permission gate that silently disables itself" is the wrong failure mode for a permission gate.

**#2 HIGH — Transcript attach race drops messages.**
`Session._start_tail` reads the whole file (`session.py:340`), parses/pushes/replays it (`:344-361`), *then* fast-forwards the tail to `path.stat().st_size` (`:378`). Any record claude appends between those two points is never rendered. Discovery fires on a 2s poll while claude is actively streaming its first turn, so the window is routinely tens to hundreds of ms of writes. Symptom: a missing assistant bubble that only appears after `Ctrl+R`. Fix: read bytes once, set `_pos = len(raw)`.

**#3 HIGH — Tab close / restart does not reliably kill `claude` (by code inspection).**
On npm installs `_resolve_claude_command` wraps `claude.cmd` in `["cmd", "/c", ...]` (`pty_wrapper.py:171-172`). `PtyWrapper.stop` only calls `proc.terminate(force=True)` (`:401-410`), which in pywinpty is `os.kill(self.pid, sig)` (`ptyprocess.py:235-249`, `269-272`) — on Windows that is `TerminateProcess` on the **direct child, `cmd.exe`**; the `node.exe` grandchild running Claude Code survives. The pseudoconsole is only closed by `PtyProcess.__del__ → close()` (`ptyprocess.py:136-166`), which cannot run while the `pty-reader` thread still references the wrapper — and that thread is blocked in `proc.read()` ("Can block if there is nothing to read", `ptyprocess.py:179-183`) because the orphaned node still holds the output pipe. Consequences: `is_alive()` (`pty_wrapper.py:99-103`) reports dead while claude runs; `on_exit` fires with cmd's code; `restart_pty` spawns a second claude in the same cwd whose JSONL competes with the orphan's; closed tabs keep burning quota. Process exit cleans up (OS closes the ConPTY, conhost terminates clients), so the app-close case is probably fine; restart/close-tab is not. `stop()` should call `proc.close(force=True)`, or the shim should be resolved to the real `node` invocation.

**#4 MEDIUM — The "never send raw Ctrl+C" rule is enforced only by a JS comment.**
`GuiBridge.send_control` (`app.py:83-85`) and `Session.send_control` (`session.py:463-466`) forward any character; `send_text`/`send_bytes` forward anything. The xterm pane's `term.onData` sends raw `\x03` via `send_text` when the user presses Ctrl+C inside it (`app.js:1583-1594`). The invariant exists only as prose (`app.js:2809-2814`). A one-line backend rejection of `\x03` in the three send paths would make the rule structural.

**#5 MEDIUM — Unlocked index arithmetic on `App._sessions` across bridge threads.**
`close_session` loops with `enumerate`, then `pop(i)` and adjusts `_active_idx` (`app.py:797-830`); `open_session` appends and sets `_active_idx = len-1` (`:752-753`). Each bridge call is its own thread (`webview/util.py:234-236`). Two near-simultaneous close clicks (double-click on `×`, or close + backend-driven `_push_sessions`) can pop the wrong index or leave `_active_idx` pointing at a stopped session. `active` should resolve by id, and mutations should take a lock.

**#6 MEDIUM — Pending-permission bookkeeping races and is DoS-able.**
`_push_permission_prompt` (HTTP handler thread, `app.py:473-479`), `resolve_permission` (bridge thread, `:549-554`), `_sweep_stale_pending_permissions` (whichever thread calls `_push_sessions`, `:668-684`) and `close_session` (`:802-812`) all mutate `_pending_permission_by_session`/`_session_by_pending_id` unlocked. Separately, the bridge is loopback with no auth (`permission_bridge.py:10`), and each request parks a `ThreadingHTTPServer` thread for up to 300s (`:157`, `:23`) — any local process can spawn unbounded blocked threads and fake banners.

**#7 MEDIUM — `/clear` is only detected on the composer path.**
`_CLEAR_CMD_RE` is checked in `Session.send_input` (`session.py:448`). `/clear` typed in the xterm pane goes through `send_text` (`app.js:1593`) and never re-pins discovery, which is latched with `lock_after_first` (`transcript.py:394-401`). The chat stays frozen on the old JSONL for the rest of the tab's life. The composer path itself relies on a fixed `time.sleep(0.5)` (`session.py:404`).

**#8 MEDIUM — Session pinning uses `st_ctime`, which is not creation time off Windows.**
`find_active_session_jsonl` and `set_pin` filter on `st_ctime` (`transcript.py:306`, `376`). On Linux/macOS that is inode change time (bumps on every write), so `min_ctime` cannot exclude an older transcript that is still being written. `gui/README.md:14` claims the code is "portability-clean … so a V2 cross-platform port is a small delta"; the pin logic is not.

**#9 MEDIUM — Dead ring buffer retains up to ~16 MB per tab.**
`PtyWrapper._ring = deque(maxlen=4096)` stores *chunks* of up to 4096 chars each (`pty_wrapper.py:81`, `185`, `206-207`). Its only consumer, the `buffer` property (`:95-97`), has zero callers (`grep` of `gui/`: the only `.buffer` hit is xterm's `term.buffer.active`, `app.js:2006`). Memory cost with no function.

**#10 MEDIUM — Whole-transcript re-read + single giant `evaluate_js` on every tab switch, close, and reload.**
`switch_to` (`app.py:770-776`), `close_session` (`:836-846`) and `_resync_frontend_state` (`:929-935`) each `read_text` the full JSONL, parse it, `json.dumps` the whole list and push it as one JS string; the DOM list is never virtualised (`app.js:733-781`). A multi-MB transcript means a multi-MB string through pywebview's `eval` path per switch.

**#11 LOW — Reader-thread exceptions look like process exit.**
`_read_loop` catches every exception with `break` (`pty_wrapper.py:188-189`) and then calls `on_exit(code)` (`:213-221`); a transient read error surfaces as "Claude Code exited (code -1)".

**#12 LOW — `TranscriptTail` cannot recover from truncation; text-mode seek with a byte offset.**
`_read_new_bytes` seeks to `_pos` and reads (`transcript.py:250-254`) with no `size < _pos` check, unlike `SubagentTracker._tail_agent` (`subagent_tracker.py:411-414`). The initial `_pos` is a byte size (`session.py:378`) fed to a text-mode `seek` — Python documents that as undefined except for `tell()` cookies; it works for UTF-8 at EOF.

**#13 LOW — Two usage fetches at startup.**
`start_services` spawns a `fetch_now` thread (`app.py:915-917`) and `UsagePoller._run` also fires immediately (`usage_fetcher.py:138-140`), doubling the first request to an endpoint the comment says not to hammer (`app.py:908-910`).

**#14 LOW — Dead-pty recovery is advertised but does not exist.**
`onPtyExit` toasts "restart from menu" (`app.js:2963`); there is no menu and `restart_pty` has zero JS callers (`app.py:105`). Same for `set_session_setting` (0 callers) whose docstring says "the frontend sends Shift+Tab chord(s)" (`app.py:715-720`) — no `Shift+Tab`/`\x1b[Z` anywhere in `gui/web/`. `session.py:273` references `App.set_session_accept_edits`, which does not exist. `accept_edits` is reachable only by hand-editing `tabs.json`.

**#15 LOW — `send_bytes` cannot send bytes ≥ 0x80.**
The latin-1 round-trip (`pty_wrapper.py:256-267`) is re-encoded as UTF-8 by pywinpty, contradicting the docstring. Only ASCII sequences are used today (`app.js:2461`).

**#16 LOW — Prompt records render literal `**`.**
`recordPromptAppeared`/`recordPromptResolution` wrap text in `**…**` (`app.js:2359`, `2392`) but `renderInline` only handles backticks (`:438-440`).

**#17 LOW — Windowed build swallows all diagnostics.**
`console=False` (`apiary_gui.spec:68`) makes `sys.stderr` `None`; every `print(..., file=sys.stderr)` (`app.py:406`, `522`, `528`, `591`, `941`, `950`; `subagent_tracker.py:146`, `367`; `scribe_aggregator warnings via app.py:449`) is silently discarded. The packaged app has no log except `permission_mcp.log` and `bubble_anomalies.jsonl`.

**#18 LOW — Flaky suite.** 1 failure in 9 runs of `poetry run pytest gui -q` (the summary line was captured; the test name was not — it did not recur in the 7 subsequent runs). Candidates are the wall-clock tests: watchdog `ThemeWatcher` (`test_theme.py:53-78`), `TranscriptTail` polling (`test_transcript.py:280-362`), mtime ordering via `time.sleep(0.05)` (`test_transcript.py:374`), `wait_for_quiet` timings (`test_pty_wrapper.py:85-135`).

**#19 LOW — Monitor attribution gap.** `interruptClaudeSession` hides the bubble with no `reason` (`app.js:2829`), so a later anomaly is attributed to whatever `lastHideReason` was before.

**Exception swallowing, in numbers.** 58 `except Exception` in non-test `gui/*.py`, 30 of them `pass`. The worst are the ones that hide configuration errors from the user: aggregator failures (`scribe_aggregator.py:229-230`, `244-245`, `session.py:221-222`) mean a broken `.claude/apiary/*-pointer.json` shows an empty sidebar forever with no message; `theme.py:122-125`; `pty_wrapper.py:188-189` (above).

**Encoding.** `encoding="utf-8"` is consistent across every `open`/`read_text`/`write_text` in `gui/` (verified by reading; no bare `open()` found). `errors="replace"` everywhere means corrupt bytes degrade silently. pywinpty already yields `str`, so the `isinstance(chunk, bytes)` branch (`pty_wrapper.py:204-205`) is defensive dead weight.

**JS error boundaries.** Every `window.apiary.on*` handler is `try/catch` with `console.error` (`app.js:2915-3081`) — good — but `console.error` is invisible unless `APIARY_GUI_DEBUG` is set (`app.py:1117`), and there is no `window.onerror`/`unhandledrejection` hook, so a bridge promise rejection in e.g. `confirmPicker` (`app.js:248`, un-caught `await`) disappears.

---

## 4. Security / safety

### What page JS can call

All 30 `GuiBridge` methods (`app.py:66-348`). The dangerous ones:

| Method | Power | Validation |
|---|---|---|
| `get_note_body(body_path)` (`app.py:206-210` → `scribe_aggregator.read_body :191-198`) | **Reads any file on disk** | `isinstance(str)` only; no allow-list, no check that the path is under a scribe dir |
| `list_directory(path)` (`picker.py:44-114`) | Enumerates any directory | none beyond existence |
| `open_session(cwd)` (`app.py:736-758`) | Spawns `claude` in any directory | `is_dir()` |
| `send_input` / `send_text` / `send_bytes` / `send_control` | Drives the live claude session (inject prompts, keystrokes, control chars) | none |
| `resolve_permission(pending_id, "allow", _, updated_input)` (`app.py:329-348`, `530-555`) | Approves a tool call **with a rewritten input** (`updatedInput`, `app.js:2312-2318`) | behavior ∈ {allow,deny} |
| `add_pasted_image(b64)` (`app.py:175-192`) | Writes arbitrary bytes into `<state>/pasted/<sid>/` | base64 validity |
| `add_quick_note` | Writes to the scribe store of the active repo | type ∈ {wishlist,todo} |
| `log_bubble_anomaly` | Appends to a fixed JSONL (`app.py:63`) | JSON object |

So an XSS in the webview equals arbitrary file read + prompt injection into Claude Code + approval of modified tool calls. XSS is the crown jewel; everything below is about whether one exists.

### XSS audit — none found

Untrusted text sources: transcript text (tool results are filtered out, but assistant text and user text are attacker-influenceable), scribe note summaries/bodies, file paths/names, permission payloads (tool inputs), pty output, theme.json, agent metadata.

- Message bodies: `renderBody` → `renderBlocks` → `renderInline` → `escapeHtml` (`app.js:430-517`) before `innerHTML` (`:772`, `:810`). Table cells go through `renderInline` (`:463`); `text-align` values are from a fixed set (`:448-455`); code-fence class is limited to `[\w-]*` (`:506`, `:512`).
- The one numeric interpolation into `innerHTML` (`toks.innerHTML`, `app.js:755-759`) uses values that Python coerced with `int()` (`transcript.py:82-85`).
- Every other sink is `textContent`: sidebar (`:951`, `:965`, `:970`, `:981`), prompt banners (`:2045`, `:2062`, `:2067`, `:2268`), MCP tool inputs (`textarea.value`, `pre.textContent` `:2140`, `:2234`), agents (`:2598`, `:2698`), picker (`:228`), file rows (`file_drop.js:79`), toasts (`:820`). Static SVG `innerHTML` only (`:223`, `file_drop.js:26-35`).
- Python→JS injection: every payload passes `json.dumps` with default `ensure_ascii=True` (`app.py:412-507`), so U+2028/2029 and quotes are escaped. pywebview's return path (`util.py:242-243`) is its own responsibility.
- `theme.json` values reach `style.setProperty` (`app.js:3001`) — CSS injection (`url(...)` fetches) from a user-owned local file; negligible.
- pty output goes to xterm.js, which sanitises escape sequences itself.

Verdict: the HTML-escaping discipline is good. The risk is the *size* of the bridge behind it, not a current hole.

### Other findings

- **Fail-open permission gate** — Bug #1. This is the single most important safety issue in the subsystem.
- **Plaintext, unbounded log of every permission-gated tool input.** `handle_tools_call` logs `REQUEST {full args}` and `DECISION` to `~/.claude/apiary_gui/permission_mcp.log` (`permission_mcp.py:224-226`, `119-125`) with no rotation. Bash commands, `Write` contents and `Edit` diffs — including any secrets in them — accumulate indefinitely in the home directory. Given the repo's own "sweep secrets" discipline this deserves a cap or redaction.
- **Loopback bridge without auth** — Bug #6 (spoofed banners / thread exhaustion; cannot approve real requests).
- **OAuth token handling** (`usage_fetcher.py:49-86`): read fresh each call, sent only to `api.anthropic.com`, never logged. Fine.
- **Drop/paste paths**: dropped paths come from pywebview's native stamp (`webview/util.py:279`), pasted files are named by uuid not by the client-supplied `name` (`file_refs.py:208-210`). No traversal.
- `--disable-cache …` WebView2 flags are set for every run, not only dev (`app.py:1050-1053`). Harmless.

---

## 5. Code quality

### Five largest functions (py + js)

| Lines | Location | Name |
|---|---|---|
| 179 | `gui/web/app.js:606` | `appendMessage` — render + per-tab state machine + anomaly monitor + tentative reconciliation + queued-ordering in one function |
| 152 | `gui/web/app.js:883` | `renderSidebar` — filter, bucket, build DOM, restore two kinds of scroll offset |
| 90 | `gui/subagent_tracker.py:149` | `_scan_once` — six responsibilities under one lock |
| 84 | `gui/app.py:1035` | `main` — MCP dispatch, env setup, mutex, import guards, window, event wiring |
| 83 | `gui/web/app.js:2626` | `renderAgentDrawer` — in-place patch path duplicated against full-rebuild path |

Next tier: `parseOptionsFrom` 80 (`prompt_detector.js:68`), composer `keydown` listener 78 (`app.js:3100`), `_start_pty` 74 (`session.py:261`), `note_parent_record` 71 (`subagent_tracker.py:252`), `list_directory` 71 (`picker.py:44`), `start_services` 67 (`app.py:852`), `Session.__init__` 66 (`session.py:118`), `filter_record` 64 (`transcript.py:89`).

### Copy-paste

- Transcript replay block appears three times: `app.py:770-776`, `836-846`, `929-935`.
- `fmtAgentElapsed` and `fmtAgentDuration` have identical bodies modulo the subtraction (`app.js:2562-2577`).
- Banner header + `✕` button + input-blocking: `app.js:2024-2037` vs `2253-2263`; teardown: `2340-2346` vs `2305-2308`.
- Three near-identical `localStorage` toggle blocks (`app.js:1055-1144`); `pty_resize` call repeated verbatim three times (`:1605-1607`, `1629-1631`, `1645-1647`).
- `_windows_drives`-style "return a dict with the same seven keys" repeated three times in `picker.list_directory` (`picker.py:53-81`, `107-114`).

### Dead code (all verified by grep across `gui/`)

- `PtyWrapper._ring` / `.buffer` (`pty_wrapper.py:81`, `95-97`, `206-207`) — no callers.
- `TranscriptTail.poke` no-op stub (`transcript.py:232-236`); `on_skip` never supplied (`session.py:371-376`).
- `GuiBridge.ping`, `list_sessions`, `restart_pty`, `set_session_setting` — 0 JS callers; `App.set_session_setting` therefore unreachable from the UI.
- `repo_registry.CONFIG_PATH` + `_load_legacy_list` — "kept for backwards-compatible import paths in tests … never read post-migration" (`repo_registry.py:22-25`, `90-107`).
- `gui/diag_pty.py` — 0 callers, top-level `from winpty import PtyProcess` (`:9`), last touched 2026-04-18, self-described "NOT for prod use".
- `scheduleDetect()` no-op "retained as hook point" (`app.js:2531`); `window.renderTabs` global plus a `typeof window.renderTabs === "function"` check on a function defined in the same closure (`app.js:90`, `2977`); `window.apiary.__sessions` written, never read (`:2970`); `void lastIdx` (`prompt_detector.js:189`).
- `AgentState.to_dict` comment says it renames keys; it returns `asdict` unchanged (`subagent_tracker.py:70-73`).
- Unused imports: `session.py:29 Message`; `transcript.py` `time`, `field`; `tabs_state.py` `asdict`, `field`; `scribe_aggregator.py` `field`; `single_instance.py` `Optional`; `capture_session.py`, `diag_pty.py` `sys`.

### Stale comments and misleading docstrings

- "Phase 3 will buffer them per-tab" (`app.js:2850`), "Phase 2: data only. Phase 3 renders the tab bar" (`:2966`) — the tab bar exists 2,900 lines earlier. "markdown-it lands later" (`:429`).
- `TranscriptTail` docstring: "Watchdog is wired in by the higher-level service" (`transcript.py:198-201`) — it is not. `parse_jsonl_lines` docstring promises a `skipped_count` (`:157-158`) that does not exist.
- State-path docstrings still say `~/.claude/apiary_gui/` (`sidebar_state.py:3`, `tabs_state.py:3`, `pty_capture.py:5`, `capture_session.py:8`) though `paths.py:3-5` moved it.
- `pty_capture.py:15` points at `gui/test_prompt_detector.py`; the file is `gui/web/test_prompt_detector.js`.
- `app.py:715-720` / `session.py:273` — see Bug #14.

### Convention breaches against `docs/standards/code-style.md`

- Constant defined between two import groups (`app.py:36-43` sits between `from gui.paths import …` and `from gui.win_notify import …`), violating the file-order rule (`code-style.md:22-31`).
- `sys.path.insert` in a non-hook module (`scribe_aggregator.py:25`) while `app.py:34` imports `scribe` without it; `code-style.md:48` reserves the hack for hooks.
- Cross-module import of a private name (`app.py:1024 from gui.pty_wrapper import _resolve_claude_command`).
- `from pathlib import Path as _P` inside a function body (`capture_session.py:49`).
- "Keep functions short" (`code-style.md:58`) — see the table above.
- Hover tooltips: 8 `title=`/`.title =` in `app.js` (`:57`, `70`, `86`, `163`, `219`, `952`, `2035`, `2261`) and 4 in `index.html` (`:25`, `57`, `58`, `158`), against the project's stated no-tooltip direction.
- `gui/requirements.txt` duplicates the poetry group but omits the `pythonnet` pin that `pyproject.toml:39` needed — two sources of truth, one stale.

### Nesting / structure

- `close_session` reaches four levels inside a `for`/`if` (`app.py:797-850`) and does teardown, index math, replay and persistence in one body.
- `_scan_repo` reaches six levels (`scribe_aggregator.py:122-173`); its status filter is a double negative that lets any *unknown* status through (`:145-149`).
- `Session.__init__` binds nine lambdas closing over `sid` (`session.py:148-167`) — a small `Emitter` object would be clearer.
- `app.css` (2,166 lines) was not reviewed line-by-line.

---

## 6. Tests

**Result:** `poetry run pytest gui -q` → **213 passed, 2 skipped** in ~13s (8 of 9 runs); one run reported **1 failed** (see Bug #18). Skips: `test_session.py:139`, `:142` ("POSIX rooted-path flavour" — Windows host). No dependency-related skips: pywebview/pywinpty/watchdog are installed in this venv. All 17 test files use `unittest` (0 import `pytest`), run via pytest with `--import-mode=importlib` (`pyproject.toml:43-44`) — conforms to `code-style.md:72`.

**JS:** `node gui/web/test_prompt_detector.js` → 31 pass; `node gui/web/test_bubble_monitor.js` → 12 pass (Node 22, built-in `node:test`). These are wired into **nothing** — no pytest bridge, no script, no CI; the only pointers are archived scribe handoffs. `app.js` (3,293 lines) and `file_drop.js` have **zero tests**: message reconciliation/queued ordering (`app.js:681-731`), the thinking-bubble state machine (`:1719-1859`), MCP banner + `updatedInput` (`:2250-2326`), arrow-driver orchestration (`:2472-2488`), markdown/table renderer (`:429-517`), usage meters (`:1176-1388`), tab switching draft handoff (`:2878-2887`).

**Python coverage by module:**
- Well covered: `transcript.py` (29 tests incl. tail threads, pin/exclude/lock semantics), `file_refs.py` (28), `permission_mcp.py` (17), `scribe_aggregator.py` (17, incl. frozen-path regressions), `pty_wrapper.py` (15 — chunking, quiet-wait, shim resolution), `usage_fetcher.py` (10), `ask_prompt.py` (9).
- Thin: `app.py` — only `log_bubble_anomaly` (5 tests, `test_app.py`) and `add_quick_note` (14, `test_quick_capture.py`). The `App` class — session list mutations, index math, permission routing, resync, shutdown — has **no tests**, and it is where Bugs #1, #5, #6 live. `session.py` — `send_bytes` and the project-key transform only; `_start_pty`, `send_input`'s paste/quiet sequence, `/clear` re-pin are untested at this level.
- Absent: `subagent_tracker.py` (497 lines, the most intricate state machine in the package — correlation by description + timestamp, three done-detection paths), `repo_registry.py` (only indirectly), `sidebar_state.py`, `single_instance.py` (one test in `test_theme.py:82`), `win_notify.py`, `win_titlebar.py`, `capture_session.py`, `packaging/*`.

**Quality of what exists:** good. Names describe behaviour, failure contracts are tested explicitly (`test_quick_capture.py:132-161`), regressions cite tickets (`test_scribe_aggregator.py:334`, `test_session.py:112`).

**Hermeticity:** mostly clean (tempdirs + `mock.patch`). Leaks: `test_picker.py:52`, `:74` assert against the real `Path.home()` and `picker_context()` reads the real registry (`picker.py:119`), so results depend on machine state; `test_theme.py:82-87` takes a real named mutex; the watchdog/tail/mtime tests depend on wall-clock sleeps and are the likely source of the observed flake.

---

## 7. Docs vs reality

| Doc claim | Reality |
|---|---|
| `gui/README.md:9` "global scribe sidebar across all registered apiary repos (read-only in V1)" | Sidebar is scoped to the active tab's cwd only (`session.py:200-204`) and now writes notes (`app.py:212-246`). `README.md:198` says "Global … for the active tab's repo" — contradicts itself in one line. |
| `gui/README.md:11` "a small pty output strip" | A full xterm.js terminal with 5,000-line scrollback (`app.js:1557-1569`). |
| `gui/README.md:35` "window title becomes `apiary [dev]`" | Title is `[dev]`; branding was removed (`paths.py:98-101`, commit `597b900`). Unprofiled title is empty. |
| `gui/README.md:39-42` reload keeps "open tabs, ptys, and sidebar state" | Composer drafts, quick-capture drafts and turn state are JS-only and lost (`app.js:1685-1716`). |
| `gui/README.md:65-70` config list incl. `apiary_repos.json`, "auto-created on first run" | `apiary_repos.json` is not read; source is `<main-apiary>/.repos/registry.json` (`repo_registry.py:3-5`, `33`). Only `theme.json`/`launch.json` are auto-created (`theme.py:59-65`). Missing from the list: `tabs.json`, `sidebar_state.json`, `composer_state.json`, `file_refs/`, `pasted/`. |
| `gui/README.md:27-28` profile "re-roots all state" | `permission_mcp.log`, `permission_mcp_config.json`, `bubble_anomalies.jsonl` are shared across profiles (`permission_mcp.py:43-44`, `app.py:63`); two profiles overwrite the same MCP config on every spawn (`session.py:281`). |
| `gui/README.md:14` "portability-clean" | `st_ctime` pinning is Windows-specific in meaning (`transcript.py:306`, `376`). |
| `README.md:200` "single double-clickable `.exe`" | One-folder bundle with `_internal/` sibling (`gui/README.md:46-47`, `build.py:106`). |
| `gui/README.md:76` fixtures → `test_prompt_detector.js` | `pty_capture.py:15` still says `gui/test_prompt_detector.py`. |
| `app.py:715-720` Shift+Tab live toggle; `session.py:273` `App.set_session_accept_edits` | Neither exists (Bug #14). |
| `gui/requirements.txt:5-7` | Omits the `pythonnet` pin and Python `<3.13` constraint that `pyproject.toml:35-39` / `SETUP.md:185-186` require. |
| `gui/README.md:3`, `app.py:1-6` cite spec `C-2026-32` | Fine; but nothing in-tree records the current feature set — the spec predates tabs, MCP, agents, file refs, quick capture. |

---

## 8. Verdicts

| Component | Verdict | Reason |
|---|---|---|
| `paths.py`, `tabs_state.py`, `sidebar_state.py`, `composer_state.py` | keep | Small, pure, tested; fix docstring paths. |
| `pty_wrapper.py` | improve | Close the pty on stop (Bug #3), reject raw Ctrl+C (Bug #4), delete `_ring`/`buffer` (Bug #9). |
| `transcript.py` | improve | Byte-mode tail with truncation handling, fix attach race (Bug #2), drop `poke`/`on_skip` or wire them, `st_ctime` portability note. |
| `session.py` | improve | Same attach race; `/clear` detection on all input paths; replace nine lambdas with an emitter; remove dead `Message` import. |
| `app.py` — `App` | improve | Lock + id-based `active`; one `_replay_active()` helper for the three copies; tests. |
| `app.py` — `GuiBridge` | keep (prune) | Sound shape; delete or wire `ping`/`list_sessions`/`restart_pty`/`set_session_setting`; allow-list `get_note_body`. |
| `permission_mcp.py` + `permission_bridge.py` | improve (urgent) | Fail closed (Bug #1); move log/config under `state_dir()`; cap the log; bound handler threads. |
| `ask_prompt.py` | keep | The right approach (structured source, not scraping); pure; tested. |
| `subagent_tracker.py` | improve | Works but is the least-tested, most heuristic module (3 done-detection paths, description+timestamp correlation); needs tests before any further change. |
| `scribe_aggregator.py` | keep | Fine; surface warnings to the UI instead of stderr. |
| `usage_fetcher.py` | keep | Clean, fail-open by design; note it depends on a private endpoint that can vanish. |
| `theme.py` | keep | Correct debounce, tested. |
| `file_refs.py` | keep | Well-reasoned (path-not-copy, owned vs referenced), best-tested module. |
| `picker.py`, `single_instance.py`, `win_*.py` | keep | Small ctypes shims; fine. |
| `pty_capture.py`, `capture_session.py` | keep (dev tool) | Useful for fixtures; fix docs. |
| `diag_pty.py` | delete | 0 callers, untouched since April, unguarded `winpty` import. |
| `packaging/` | improve | Pin PyInstaller in a `build` group, stamp the commit into the manifest/exe, record the xterm hash. |
| `prompt_detector.js` | improve → shrink | Already reduced to the glyphed path (commit `a9787b6`); keep shrinking as MCP/Ask paths take over. Node-tested. |
| `bubble_monitor.js` | keep (temporary) | Legitimate diagnostic; delete once `bubble_anomalies.jsonl` yields a root cause. |
| `file_drop.js` | keep | Small, single-purpose; add a Node test. |
| `app.js` | rewrite (as a split, not from scratch) | 3.3k-line IIFE, 20+ shared `let`s, five timers, zero tests. The logic is mostly right; the structure prevents testing it. |
| `app.css` | improve | Not reviewed in depth; 2.2k lines suggests the same consolidation need. |
| Tests | improve | Good Python unit tests where they exist; no `App` tests, no JS harness, one flake. |
| `gui/README.md` / `README.md` GUI section | improve | Nine concrete mismatches above. |

**Overall verdict.** The GUI is competently built — the JSONL-as-truth decision, Python-authoritative file manifests, the structured AskUserQuestion path, the MCP permission route, and the `L-2026-133` lesson ("don't fix against guessed fixtures") all show real engineering judgement. But it is a **sink competing with the core toolkit, not a strategic asset**, on the evidence:

- 93 commits in four months (`git log -- gui`), the large majority chasing Claude Code TUI behaviour: three "Phase 2" iterations of a glyph-less menu scraper that were then deleted (`a9787b6`), a thinking-bubble bug that could not be reproduced and got an anomaly-monitoring subsystem instead of a fix, paste-truncation probes added and removed, ESC-burst cadences tuned against upstream issues.
- Twelve undocumented Claude Code internals across nine files, with no adapter boundary; each Claude Code release is a potential breakage with no test signal (the JS tests are not even wired to a runner).
- Single platform, single user, no CI build, a permission gate that fails open, and a pty teardown that (by inspection) orphans the CLI.
- It is the sanctioned exception to the repo's one hard rule (`code-style.md:17`), and that exception already forced a Python `<3.13` constraint for GUI installs (`pyproject.toml:39`, `SETUP.md:185`).

Recommendation: keep it as a personal tool with a fixed cost ceiling. Fix the three HIGH bugs, make the MCP permission path the default, split `app.js` so it can be tested, and freeze feature work on anything that scrapes the TUI. Do not let GUI needs drive core-toolkit architecture.

---

## 9. Top 10 recommended changes (ranked by value ÷ effort)

| # | Change | Rationale | Effort |
|---|---|---|---|
| 1 | **Fail-closed MCP permission server**: `decide()` returns deny when `APIARY_PERMISSION_MCP_URL` is unset unless an explicit `APIARY_PERMISSION_MCP_ALLOW_ALL=1` test flag is present; set `APIARY_PERMISSION_MCP=1` only *after* `bridge.start()` succeeds. | Bug #1 — a permission gate must never silently become a rubber stamp. | S |
| 2 | **Fix the transcript attach race**: read bytes once in `_start_tail`, decode, set `tail._pos = len(raw)`; make `TranscriptTail` byte-mode with the same truncation check `SubagentTracker` has. | Bug #2/#12 — lost messages are the core feature failing. | S |
| 3 | **Backend guard against raw Ctrl+C** in `send_control`/`send_text`/`send_bytes` (reject `\x03`), with a test. | Bug #4 — makes the project's own rule structural instead of a comment. | S |
| 4 | **Close the pty on stop** (`proc.close(force=True)` after terminate) and verify the grandchild dies; make `is_alive()` reflect the real CLI process. | Bug #3 — orphaned claude processes burn quota and corrupt discovery on restart. | M |
| 5 | **Unify state under `state_dir()`** (permission log/config, bubble log), cap `permission_mcp.log` (size/rotation, redact `Write.content`). | Profile isolation is currently false; the log is an unbounded plaintext record of tool inputs. | S |
| 6 | **Dead-code and docs purge**: `_ring`/`buffer`, `poke`, `on_skip`, `diag_pty.py`, unused bridge methods (or add a restart button and fix the toast), legacy `CONFIG_PATH`, stale "Phase" comments, unused imports; reconcile the nine README mismatches and `requirements.txt`. | Cheap, removes ~300 lines and several lies. | S |
| 7 | **Lock `App` mutations and resolve `active` by session id**, dedupe the three replay blocks into `_replay_active()`. Add `test_app_sessions.py` covering open/switch/close index math and permission routing with a fake `Session`. | Bugs #5/#6/#10; the untested class is the riskiest one. | M |
| 8 | **Wire the JS tests into the test run** (a pytest test that shells out to `node` and skips if absent) and extract `appendMessage`'s reconciliation and the thinking-bubble state machine into pure, Node-tested modules like `prompt_detector.js`. | 80% of the frontend logic has no test signal against Claude Code changes. | M |
| 9 | **Pin the build**: PyInstaller in a `[tool.poetry.group.build]`, stamp the git SHA into the manifest/`SERVER_VERSION`, record the xterm hash next to the vendored file. | Reproducibility and "which build am I running" for the exe path. | S |
| 10 | **Split `app.js`** into per-concern IIFE modules (tabs, picker, chat, sidebar, usage, terminal+prompts, agents, composer) sharing one small event bus and one `dispatch()` for pushes. | The monolith is the reason #8 is hard; do it after #8's extractions so the split lands with tests. | L |
