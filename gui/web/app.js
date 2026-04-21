// apiary GUI frontend.

(() => {
  // --- DOM handles ----------------------------------------------------------
  const tabsEl = document.getElementById("tabs");
  const messagesEl = document.getElementById("messages");
  const emptyStateEl = document.getElementById("empty-state");
  const emptyPickBtn = document.getElementById("empty-pick");
  const statusEl = document.getElementById("status");
  const totIn = document.getElementById("tot-in");
  const totOut = document.getElementById("tot-out");
  const totCacheR = document.getElementById("tot-cache-r");
  const totCacheW = document.getElementById("tot-cache-w");
  const totRaw = document.getElementById("tot-raw");
  const totWeighted = document.getElementById("tot-weighted");
  const modelNameEl = document.getElementById("model-name");
  const toastsEl = document.getElementById("toasts");
  const ptyStripWrapEl = document.getElementById("pty-strip-wrap");
  const ptyTermEl = document.getElementById("pty-term");
  const ptyToggleEl = document.getElementById("pty-toggle");
  const ptyToggleLabelEl = document.getElementById("pty-toggle-label");
  const ptyUnreadEl = document.getElementById("pty-unread");
  const promptBannerEl = document.getElementById("prompt-banner");
  const handoffBannerEl = document.getElementById("handoff-banner");
  const handoffBannerTextEl = document.getElementById("handoff-banner-text");
  const handoffBannerBtnEl = document.getElementById("handoff-banner-btn");
  const handoffBannerDismissEl = document.getElementById("handoff-banner-dismiss");
  const inputEl = document.getElementById("input");
  const INPUT_PLACEHOLDER_DEFAULT = inputEl.getAttribute("placeholder") || "";
  const sidebarSearchEl = document.getElementById("sidebar-search");
  const sidebarListEl = document.getElementById("sidebar-list");

  // --- tab bar --------------------------------------------------------------
  // Backend sends onSessions(list) whenever tabs open/close/switch. We render
  // a chip per session plus a "+" button that opens a folder picker.
  //
  // Also keeps a cache of each session's per-tab settings so the prompt-detector
  // poll can read allow_self_edits without round-tripping through the bridge.
  const sessionSettings = new Map();  // sid -> { accept_edits, allow_self_edits }

  function getSessionSettings(sid) {
    return sessionSettings.get(sid) || { accept_edits: false, allow_self_edits: false };
  }

  function renderTabs(sessions) {
    if (!Array.isArray(sessions)) return;
    tabsEl.innerHTML = "";
    // Empty-state toggle: when no tabs, hide messages list and show the
    // "pick a directory" CTA. When there are tabs, flip it back.
    if (sessions.length === 0) {
      emptyStateEl.classList.remove("hidden");
      messagesEl.style.display = "none";
    } else {
      emptyStateEl.classList.add("hidden");
      messagesEl.style.display = "";
    }
    // Refresh the settings cache from the full snapshot. Any missing sid has
    // been closed; clear its entry to avoid stale reads.
    const seenSids = new Set();
    for (const s of sessions) {
      seenSids.add(s.session_id);
      sessionSettings.set(s.session_id, {
        accept_edits: !!s.accept_edits,
        allow_self_edits: !!s.allow_self_edits,
      });
    }
    for (const sid of Array.from(sessionSettings.keys())) {
      if (!seenSids.has(sid)) sessionSettings.delete(sid);
    }
    for (const s of sessions) {
      const tab = document.createElement("div");
      tab.className = "tab" + (s.active ? " active" : "");
      tab.dataset.sid = s.session_id;
      // Surface active per-tab permissions in the tooltip + via data flags
      // so CSS can show a subtle dot/highlight when a tab is permissive.
      const flags = [];
      if (s.accept_edits) flags.push("auto-accept edits");
      if (s.allow_self_edits) flags.push("allow self-edits");
      tab.title = flags.length ? `${s.cwd}\n[${flags.join(", ")}]` : s.cwd;
      if (s.accept_edits) tab.dataset.acceptEdits = "1";
      if (s.allow_self_edits) tab.dataset.allowSelfEdits = "1";

      const label = document.createElement("span");
      label.className = "tab-label";
      label.textContent = s.label || s.cwd || "session";
      tab.appendChild(label);

      const cog = document.createElement("button");
      cog.className = "tab-cog";
      cog.type = "button";
      cog.textContent = "⚙";
      cog.title = "per-tab permission settings";
      cog.addEventListener("click", (e) => {
        e.stopPropagation();
        openSettingsPopover(s, cog);
      });
      tab.appendChild(cog);

      const close = document.createElement("button");
      close.className = "tab-close";
      close.type = "button";
      close.textContent = "×";
      close.title = "close tab";
      close.addEventListener("click", (e) => {
        e.stopPropagation();
        closeTab(s.session_id);
      });
      tab.appendChild(close);

      tab.addEventListener("click", () => {
        if (!s.active) switchTab(s.session_id);
      });
      tabsEl.appendChild(tab);
    }
    const addBtn = document.createElement("button");
    addBtn.className = "tab-new";
    addBtn.type = "button";
    addBtn.textContent = "+";
    addBtn.title = "open a new session in another directory";
    addBtn.addEventListener("click", openNewTab);
    tabsEl.appendChild(addBtn);
  }
  window.renderTabs = renderTabs;

  // --- per-tab settings popover --------------------------------------------
  let settingsPopoverEl = null;

  function closeSettingsPopover() {
    if (settingsPopoverEl) {
      settingsPopoverEl.remove();
      settingsPopoverEl = null;
      document.removeEventListener("click", onDocClickForPopover, true);
    }
  }

  function onDocClickForPopover(e) {
    if (!settingsPopoverEl) return;
    if (settingsPopoverEl.contains(e.target)) return;
    closeSettingsPopover();
  }

  function openSettingsPopover(session, anchorEl) {
    closeSettingsPopover();
    const popover = document.createElement("div");
    popover.className = "tab-settings-popover";
    popover.addEventListener("click", (e) => e.stopPropagation());

    const title = document.createElement("div");
    title.className = "tab-settings-title";
    title.textContent = "Per-tab permissions";
    popover.appendChild(title);

    popover.appendChild(renderSettingRow(
      session.session_id,
      "accept_edits",
      "Auto-accept edits",
      "Cycles claude's permission mode via Shift+Tab (session history preserved). Also applied via --permission-mode acceptEdits on the next spawn.",
      !!session.accept_edits,
    ));
    popover.appendChild(renderSettingRow(
      session.session_id,
      "allow_self_edits",
      "Allow Claude to edit its own settings (.claude/)",
      "Auto-click 'Yes' on the harness protect-self prompt when it appears.",
      !!session.allow_self_edits,
    ));

    // Anchor under the cog button — relative to the tabs nav so horizontal
    // scroll in the tab strip doesn't detach the popover.
    const anchor = anchorEl.getBoundingClientRect();
    popover.style.position = "fixed";
    popover.style.top = `${Math.round(anchor.bottom + 4)}px`;
    popover.style.left = `${Math.round(anchor.left)}px`;
    document.body.appendChild(popover);
    settingsPopoverEl = popover;

    // Defer the document listener so this click doesn't immediately close.
    setTimeout(() => {
      document.addEventListener("click", onDocClickForPopover, true);
    }, 0);
  }

  function renderSettingRow(sid, key, label, tooltip, initialValue) {
    const row = document.createElement("label");
    row.className = "tab-settings-row";
    row.title = tooltip;
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!initialValue;
    cb.addEventListener("change", async () => {
      if (!bridgeReady()) return;
      try {
        const ok = await window.pywebview.api.set_session_setting(sid, key, cb.checked);
        if (!ok) { cb.checked = !cb.checked; return; }
        // accept_edits flips claude's live permission mode via Shift+Tab
        // chords so session history survives. The stored value is still
        // consumed on the NEXT spawn (new tab / explicit restart) via
        // --permission-mode acceptEdits.
        if (key === "accept_edits") {
          cyclePermissionModeToTarget(cb.checked ? "acceptEdits" : "default");
        }
      } catch (e) {
        console.error("set_session_setting failed", e);
        cb.checked = !cb.checked;
      }
    });
    row.appendChild(cb);
    const text = document.createElement("span");
    text.textContent = label;
    row.appendChild(text);
    return row;
  }

  // Detect claude-code's live permission mode from the rendered TUI footer
  // and cycle Shift+Tab (CSI Z, "\x1b[Z") to reach `target`.
  //
  // Claude-code 2.1.83+ has FOUR modes (https://code.claude.com/docs/en/permission-modes):
  //   default      — no footer; classifier + normal prompts
  //   acceptEdits  — auto-accepts edit tool calls
  //   plan         — read-only planning mode
  //   auto         — no prompts, classifier reviews each action. Must be
  //                  enabled first via `claude --enable-auto-mode` (Team /
  //                  Enterprise / API plan, Sonnet/Opus 4.6). First entry
  //                  shows a confirmation; declining drops auto from the
  //                  cycle.
  //
  // Cycle length isn't fixed (3 or 4 slots depending on whether auto is
  // enabled + accepted), so press + re-detect + re-check iteratively rather
  // than assume a fixed press count. Max 5 iterations caps the worst case.
  function detectPermissionMode() {
    const lines = readScreenLines();
    const tail = lines.slice(-40).join("\n");
    if (/\baccept[\s_-]*edits?\b/i.test(tail)) return "acceptEdits";
    if (/\bplan\s*mode\b/i.test(tail)) return "plan";
    if (/\bauto\s*mode\b/i.test(tail)) return "auto";
    return "default";
  }

  function labelForMode(mode) {
    if (mode === "acceptEdits") return "accept-edits";
    if (mode === "plan") return "plan";
    if (mode === "auto") return "auto";
    return "default";
  }

  const SHIFT_TAB_WAIT_MS = 200;

  async function cyclePermissionModeToTarget(target) {
    if (!bridgeReady()) return;
    const start = detectPermissionMode();
    if (start === target) {
      toast(`Permission mode already ${labelForMode(target)}`, "");
      return;
    }
    let current = start;
    let presses = 0;
    for (let i = 0; i < 5 && current !== target; i++) {
      try { window.pywebview.api.send_text("\x1b[Z"); } catch (_) {}
      presses += 1;
      await new Promise(r => setTimeout(r, SHIFT_TAB_WAIT_MS));
      current = detectPermissionMode();
    }
    if (current === target) {
      toast(
        `Permission mode: ${labelForMode(start)} → ${labelForMode(target)} (${presses}× Shift+Tab)`,
        "",
      );
    } else {
      toast(
        `Couldn't reach ${labelForMode(target)} after ${presses}× Shift+Tab (now ${labelForMode(current)})`,
        "error",
      );
    }
  }

  async function switchTab(sid) {
    if (!bridgeReady()) return;
    try { await window.pywebview.api.switch_session(sid); } catch (e) { console.error(e); }
  }

  async function closeTab(sid) {
    if (!bridgeReady()) return;
    try { await window.pywebview.api.close_session(sid); } catch (e) { console.error(e); }
  }

  async function openNewTab() {
    if (!bridgeReady()) return;
    try {
      const dir = await window.pywebview.api.pick_directory();
      if (!dir) return;
      await window.pywebview.api.open_session(dir);
    } catch (e) {
      console.error("open new tab failed", e);
    }
  }
  emptyPickBtn.addEventListener("click", openNewTab);

  // --- chat state -----------------------------------------------------------
  const totals = { input: 0, output: 0, cache_read: 0, cache_write: 0 };
  const seen = new Set();
  // Sticky-bottom semantics: once the user scrolls up, we stop auto-scrolling
  // until they manually scroll back to bottom. This survives smooth-scroll
  // races and content-visibility layout timing better than re-checking on every
  // append.
  let sticky = true;
  let pendingScroll = false;
  let currentModel = "";

  // --- token weighting ------------------------------------------------------
  // Opus 4.x relative pricing (input-equivalent units):
  //   in:1 · out:5 · cache_r:0.1 · cache_w(5min):1.25
  function weightedTotal(t) {
    return t.input * 1 + t.output * 5 + t.cache_read * 0.1 + t.cache_write * 1.25;
  }
  function rawTotal(t) {
    return t.input + t.output + t.cache_read + t.cache_write;
  }
  function fmtNum(n) {
    return Math.round(n).toLocaleString();
  }

  function fmtTimestamp(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return `${hh}:${mm}:${ss}`;
  }

  function humanizeModel(id) {
    if (!id) return "—";
    // claude-opus-4-7 → opus 4.7 ; claude-sonnet-4-6-1m → sonnet 4.6 (1m)
    const m = id.match(/claude-([a-z]+)-(\d+)-(\d+)(?:\[(\w+)\])?/i);
    if (!m) return id;
    const variant = m[4] ? ` (${m[4]})` : "";
    return `${m[1]} ${m[2]}.${m[3]}${variant}`;
  }

  function setModel(model) {
    if (!model || model === currentModel) return;
    currentModel = model;
    modelNameEl.textContent = humanizeModel(model);
  }

  // --- markdown render (Phase 1 minimal — markdown-it lands later) ---------
  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function renderInline(text) {
    return escapeHtml(text).replace(/`([^`\n]+)`/g, "<code>$1</code>");
  }
  function renderBody(text) {
    const fence = /```([\w-]*)\n([\s\S]*?)```/g;
    let html = "";
    let last = 0;
    let m;
    while ((m = fence.exec(text)) !== null) {
      html += renderInline(text.slice(last, m.index));
      html += `<pre><code class="lang-${escapeHtml(m[1] || "")}">${escapeHtml(m[2])}</code></pre>`;
      last = m.index + m[0].length;
    }
    html += renderInline(text.slice(last));
    return html;
  }

  // --- scroll handling ------------------------------------------------------
  function isAtBottom(tol = 32) {
    return messagesEl.scrollTop + messagesEl.clientHeight + tol >= messagesEl.scrollHeight;
  }
  // When we scroll programmatically the browser still fires a scroll event,
  // and the subsequent isAtBottom() reading can be stale (content-visibility
  // layout hasn't settled) — causing `sticky` to flip false and block the
  // next auto-scroll. Gate the sticky update to user-originated scrolls.
  let programmaticScroll = false;
  function scrollToBottom() {
    programmaticScroll = true;
    messagesEl.scrollTop = messagesEl.scrollHeight;
    // Clear on the microtask after the scroll event drains.
    setTimeout(() => { programmaticScroll = false; }, 0);
  }
  messagesEl.addEventListener("scroll", () => {
    if (programmaticScroll) return;
    sticky = isAtBottom();
  });
  function maybeScroll() {
    if (!sticky) return;
    if (pendingScroll) return;
    pendingScroll = true;
    // Double-rAF so layout (incl. content-visibility expansion) settles first.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        pendingScroll = false;
        scrollToBottom();
      });
    });
  }

  function setStatus(text) {
    if (!text) {
      statusEl.classList.add("hidden");
      statusEl.textContent = "";
    } else {
      statusEl.classList.remove("hidden");
      statusEl.textContent = text;
    }
  }

  // --- token totals ---------------------------------------------------------
  function refreshTotalsBadges() {
    totIn.textContent = totals.input.toLocaleString();
    totOut.textContent = totals.output.toLocaleString();
    totCacheR.textContent = totals.cache_read.toLocaleString();
    totCacheW.textContent = totals.cache_write.toLocaleString();
    totRaw.textContent = fmtNum(rawTotal(totals));
    totWeighted.textContent = fmtNum(weightedTotal(totals));
  }
  function addTokens(t) {
    if (!t) return;
    totals.input += t.input || 0;
    totals.output += t.output || 0;
    totals.cache_read += t.cache_read || 0;
    totals.cache_write += t.cache_write || 0;
    refreshTotalsBadges();
  }
  function resetTotals() {
    totals.input = totals.output = totals.cache_read = totals.cache_write = 0;
    refreshTotalsBadges();
  }
  function clearMessages() {
    messagesEl.innerHTML = "";
    seen.clear();
    resetTotals();
    sticky = true;
    currentModel = "";
    modelNameEl.textContent = "—";
  }

  // --- message render -------------------------------------------------------
  function appendMessage(msg) {
    if (msg.uuid && seen.has(msg.uuid)) return;
    if (msg.uuid) seen.add(msg.uuid);

    // Optimistic-render reconciliation: when a real user message arrives with
    // text that matches an outstanding tentative, replace the tentative in
    // place so the chronology holds. Previously we removed the tentative and
    // appended the real message at the end — but if an assistant message had
    // already landed between the tentative and the reconciliation, the real
    // user message would drop *below* the assistant reply.
    let insertAnchor = null;
    let inheritedQueued = false;
    if (msg.role === "user" && msg.text) {
      const tentatives = messagesEl.querySelectorAll("li.msg.user.tentative");
      let matched = null;
      for (const el of tentatives) {
        if (el.dataset.text === msg.text) {
          matched = el;
          break;
        }
      }
      if (matched) {
        insertAnchor = matched.nextSibling;
        inheritedQueued = matched.classList.contains("queued");
        matched.remove();
      } else if (tentatives.length > 0) {
        // No exact text match but outstanding tentatives exist. Something is
        // out of sync (content normalized between optimistic render and JSONL
        // write, or a stale tentative from a prior turn). Sweep them so we
        // don't leave duplicates on screen. Warn so the root cause can be
        // chased next recurrence.
        console.warn("[apiary] tentative reconcile fallback", {
          tentativeTexts: Array.from(tentatives, el => el.dataset.text),
          msgText: msg.text,
        });
        const last = tentatives[tentatives.length - 1];
        insertAnchor = last.nextSibling;
        inheritedQueued = Array.from(tentatives).some(el => el.classList.contains("queued"));
        tentatives.forEach(el => el.remove());
      }
    } else if (msg.role === "assistant") {
      // Queued-message ordering: if the user sent follow-up messages while
      // claude was still working on a prior turn, those messages are marked
      // .queued. Insert this reply BEFORE the first queued user message so
      // the chronology stays [user1][reply1][user2][reply2] instead of
      // [user1][user2][reply1][reply2]. Then un-mark that user message —
      // it's now the "current" turn, not a queued one, and the next
      // assistant reply should land after it (before the next queued msg).
      const firstQueued = messagesEl.querySelector("li.msg.user.queued");
      if (firstQueued) {
        insertAnchor = firstQueued;
        firstQueued.classList.remove("queued");
      }
    }

    const li = document.createElement("li");
    li.className = `msg ${msg.role}`;
    if (inheritedQueued) li.classList.add("queued");
    li.dataset.uuid = msg.uuid || "";

    const meta = document.createElement("div");
    meta.className = "msg-meta";

    const left = document.createElement("span");
    left.className = "msg-role";
    left.textContent = msg.role;
    meta.appendChild(left);

    const right = document.createElement("span");
    right.className = "msg-meta-right";
    const ts = document.createElement("span");
    ts.textContent = fmtTimestamp(msg.timestamp);
    right.appendChild(ts);

    if (msg.role === "assistant" && msg.tokens) {
      const toks = document.createElement("span");
      toks.className = "msg-tokens";
      toks.innerHTML =
        ` <span class="t">in ${msg.tokens.input}</span>` +
        ` <span class="t">out ${msg.tokens.output}</span>` +
        ` <span class="t">cache r ${msg.tokens.cache_read}</span>` +
        ` <span class="t">cache w ${msg.tokens.cache_write}</span>`;
      right.appendChild(toks);
    }
    meta.appendChild(right);
    li.appendChild(meta);

    // Click anywhere on meta row toggles per-message token visibility.
    meta.addEventListener("click", () => li.classList.toggle("expanded"));

    const body = document.createElement("div");
    body.className = "msg-body";
    body.innerHTML = renderBody(msg.text || "");
    li.appendChild(body);

    if (insertAnchor && insertAnchor.parentNode === messagesEl) {
      messagesEl.insertBefore(li, insertAnchor);
    } else {
      messagesEl.appendChild(li);
    }

    if (msg.role === "assistant") {
      if (msg.tokens) addTokens(msg.tokens);
      if (msg.model) setModel(msg.model);
      lastAsstMsgAt = Date.now();
      // Claude finished talking — clear the unread badge so the auto-expand
      // heuristic doesn't fire on stale pty activity that preceded the response.
      if (ptyStripWrapEl.classList.contains("collapsed")) {
        unreadCount = 0;
        ptyUnreadEl.classList.add("hidden");
        ptyUnreadEl.textContent = "";
      }
      // Route on stop_reason. "end_turn" = claude is done → kill the bubble
      // immediately. "tool_use" (or other mid-turn reasons) = more messages
      // still coming → hide the current DOM node but keep waitingForAssistant
      // true and re-spawn a fresh bubble right away so there's no visual gap
      // between chained assistant messages. The 15s idle timer is the final
      // safety net for sessions where stop_reason never resolves to end_turn.
      if (typeof hideThinkingBubble === "function") {
        if (msg.stop_reason === "end_turn") {
          hideThinkingBubble(true);
        } else {
          hideThinkingBubble(false);
          if (typeof ensureThinkingBubble === "function") ensureThinkingBubble();
        }
      }
    }

    maybeScroll();
  }

  // Optimistic render: synthesize a user message and append immediately on send.
  // Marked .tentative so the eventual JSONL-derived record replaces it cleanly.
  // `queued` marks messages the user sent while claude was still working on a
  // prior turn — the marker rides through reconciliation so assistant replies
  // land in the correct chronological position.
  function appendTentativeUserMessage(text, queued) {
    const li = document.createElement("li");
    li.className = "msg user tentative" + (queued ? " queued" : "");
    li.dataset.text = text;

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const left = document.createElement("span");
    left.className = "msg-role";
    left.textContent = "user";
    const right = document.createElement("span");
    right.className = "msg-meta-right";
    right.textContent = fmtTimestamp(new Date().toISOString());
    meta.appendChild(left);
    meta.appendChild(right);
    li.appendChild(meta);

    const body = document.createElement("div");
    body.className = "msg-body";
    body.innerHTML = renderBody(text || "");
    li.appendChild(body);

    messagesEl.appendChild(li);
    maybeScroll();
  }

  function toast(text, kind) {
    const el = document.createElement("div");
    el.className = `toast ${kind || ""}`;
    el.textContent = text;
    toastsEl.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  // Insert text at the current caret position in the chat input and focus it.
  // Used by sidebar ID clicks so the user can compose messages that reference
  // notes ("see T-2026-153") without typing the ID by hand.
  function insertIntoInput(text) {
    if (!text) return;
    const start = inputEl.selectionStart ?? inputEl.value.length;
    const end = inputEl.selectionEnd ?? inputEl.value.length;
    const before = inputEl.value.slice(0, start);
    const after = inputEl.value.slice(end);
    // Pad with spaces so the ID doesn't glue to surrounding words.
    const needsLeadSpace = before.length > 0 && !/\s$/.test(before);
    const needsTrailSpace = after.length > 0 && !/^\s/.test(after);
    const insertText = (needsLeadSpace ? " " : "") + text + (needsTrailSpace ? " " : "");
    inputEl.value = before + insertText + after;
    const caret = start + insertText.length;
    inputEl.focus();
    inputEl.setSelectionRange(caret, caret);
  }

  // --- sidebar --------------------------------------------------------------
  let allNotes = [];
  let sidebarFilter = "";
  let sidebarSearchTimer = null;
  // Group key → collapsed boolean. Hydrated from backend on startup.
  let collapsed = new Set();
  // Notes the user has expanded inline. Survives sidebar re-renders (which
  // happen every 5s as the aggregator polls).
  const expandedNotes = new Set();
  // Cached body text keyed by note display_id, so re-renders don't re-fetch.
  const bodyCache = new Map();

  // Group definitions: each entry { key, label, match }. Order = render order.
  const GROUPS = [
    { key: "todo-ready", label: "todo (ready)", match: (n) => n.type === "todo" && (n.status === "active" || n.status === "open" || n.status === "") },
    { key: "todo-deferred", label: "todo (deferred)", match: (n) => n.type === "todo" && (n.status === "deferred" || n.status === "blocked") },
    { key: "wishlist", label: "wishlist", match: (n) => n.type === "wishlist" },
    { key: "blocker", label: "blocker", match: (n) => n.type === "blocker" },
    { key: "context", label: "context", match: (n) => n.type === "context" },
    { key: "decision", label: "decision", match: (n) => n.type === "decision" },
    { key: "reference", label: "reference", match: (n) => n.type === "reference" },
    { key: "general", label: "general", match: (n) => n.type === "general" },
    { key: "handoff", label: "handoff", match: (n) => n.type === "handoff" },
    { key: "learning", label: "learning", match: (n) => n.type === "learning" },
  ];

  function persistCollapsed() {
    if (!bridgeReady()) return;
    try {
      window.pywebview.api.save_sidebar_collapsed(Array.from(collapsed));
    } catch (e) {
      console.error("save_sidebar_collapsed failed", e);
    }
  }

  function renderSidebar() {
    const f = sidebarFilter.trim().toLowerCase();
    const filtered = f
      ? allNotes.filter(
          (n) =>
            (n.summary || "").toLowerCase().includes(f) ||
            (n.display_id || "").toLowerCase().includes(f) ||
            (n.repo_label || "").toLowerCase().includes(f)
        )
      : allNotes;

    const buckets = new Map();
    for (const g of GROUPS) buckets.set(g.key, []);
    for (const n of filtered) {
      for (const g of GROUPS) {
        if (g.match(n)) {
          buckets.get(g.key).push(n);
          break;
        }
      }
    }

    const frag = document.createDocumentFragment();
    for (const g of GROUPS) {
      const items = buckets.get(g.key);
      if (items.length === 0) continue;

      const group = document.createElement("div");
      group.className = "sidebar-group";
      if (collapsed.has(g.key)) group.classList.add("collapsed");

      const head = document.createElement("div");
      head.className = "sidebar-group-head";

      const left = document.createElement("span");
      const chev = document.createElement("span");
      chev.className = "chevron";
      chev.textContent = "▾";
      left.appendChild(chev);
      const label = document.createElement("span");
      label.textContent = g.label;
      left.appendChild(label);

      const right = document.createElement("span");
      right.textContent = String(items.length);

      head.appendChild(left);
      head.appendChild(right);

      head.addEventListener("click", () => {
        if (collapsed.has(g.key)) collapsed.delete(g.key);
        else collapsed.add(g.key);
        group.classList.toggle("collapsed");
        persistCollapsed();
      });

      group.appendChild(head);

      const body = document.createElement("div");
      body.className = "sidebar-group-body";
      for (const n of items) {
        const item = document.createElement("div");
        item.className = "sidebar-item";
        item.dataset.bodyPath = n.body_path || "";
        item.dataset.displayId = n.display_id;

        const idEl = document.createElement("div");
        idEl.className = "sidebar-item-id";
        idEl.textContent = n.display_id;
        idEl.title = "click to insert this ID into the chat input";
        idEl.addEventListener("click", (e) => {
          e.stopPropagation();
          insertIntoInput(n.display_id);
        });
        item.appendChild(idEl);

        const sumEl = document.createElement("div");
        sumEl.className = "sidebar-item-summary";
        // Prefer brief_summary — it's the one-sentence headline produced by
        // scribe's derivation, so mid-word cutoffs like "permissi" don't
        // leak into the sidebar. Fall back to the legacy `summary` for any
        // note that predates backfill (should be none after migration).
        sumEl.textContent = n.brief_summary || n.summary || "(no summary)";
        item.appendChild(sumEl);

        const repoEl = document.createElement("div");
        repoEl.className = "sidebar-item-repo";
        repoEl.textContent = n.repo_label;
        item.appendChild(repoEl);

        const bodyEl = document.createElement("pre");
        bodyEl.className = "sidebar-item-body";
        item.appendChild(bodyEl);

        // Restore expansion state across re-renders (aggregator polls every 5s).
        if (expandedNotes.has(n.display_id)) {
          item.classList.add("expanded");
          if (bodyCache.has(n.display_id)) {
            bodyEl.textContent = bodyCache.get(n.display_id);
            bodyEl.dataset.loaded = "1";
          } else if (n.has_body) {
            // Cache miss but the user wanted it expanded — kick off a load.
            loadNoteBody(n, bodyEl);
          } else {
            bodyEl.textContent = n.summary || "(no body)";
          }
        }

        item.addEventListener("click", (e) => {
          // Allow text selection inside an open body without collapsing it.
          if (item.classList.contains("expanded") && bodyEl.contains(e.target)) return;
          toggleNote(item, n, bodyEl);
        });
        body.appendChild(item);
      }
      group.appendChild(body);
      frag.appendChild(group);
    }

    sidebarListEl.innerHTML = "";
    sidebarListEl.appendChild(frag);
  }

  // --- usage meters (T-2026-25) --------------------------------------------
  // Backend (gui/usage_fetcher.UsagePoller) hits the undocumented
  // /api/oauth/usage endpoint every 60s and pushes the payload here via
  // window.apiary.onUsage. Payload shape:
  //   { five_hour: {utilization, resets_at},
  //     seven_day: {utilization, resets_at},
  //     extra_usage: {used_credits, monthly_limit, utilization, currency}, ... }
  // Null means the fetch failed (missing creds / 429 / timeout); we show
  // stale numbers with a "stale" tint rather than flashing empty.
  const usageMetersEl = document.getElementById("usage-meters");
  const usageBodyEl = document.getElementById("usage-body");
  const usageVariantToggleEl = document.getElementById("usage-variant-toggle");
  const usageCreditsToggleEl = document.getElementById("usage-credits-toggle");
  const usageTitleEl = document.getElementById("usage-title");
  let lastUsagePayload = null;
  let usageStale = false;
  // Credits visibility is purely a view preference — persist in localStorage
  // so it survives reload without needing a backend round-trip. Default ON.
  let showCredits = (() => {
    try { return localStorage.getItem("apiary.usage.showCredits") !== "0"; }
    catch (_) { return true; }
  })();
  let usageCollapsed = (() => {
    try { return localStorage.getItem("apiary.usage.collapsed") === "1"; }
    catch (_) { return false; }
  })();

  function reflectCreditsToggle() {
    if (!usageCreditsToggleEl) return;
    usageCreditsToggleEl.classList.toggle("active", showCredits);
    usageCreditsToggleEl.setAttribute(
      "aria-pressed", showCredits ? "true" : "false"
    );
  }
  reflectCreditsToggle();

  function reflectUsageCollapsed() {
    if (usageMetersEl) usageMetersEl.classList.toggle("collapsed", usageCollapsed);
    if (usageTitleEl) usageTitleEl.setAttribute("aria-expanded", usageCollapsed ? "false" : "true");
  }
  reflectUsageCollapsed();

  if (usageTitleEl) {
    const toggleCollapsed = () => {
      usageCollapsed = !usageCollapsed;
      try { localStorage.setItem("apiary.usage.collapsed", usageCollapsed ? "1" : "0"); }
      catch (_) {}
      reflectUsageCollapsed();
    };
    usageTitleEl.addEventListener("click", toggleCollapsed);
    usageTitleEl.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggleCollapsed();
      }
    });
  }

  if (usageVariantToggleEl) {
    usageVariantToggleEl.addEventListener("click", () => {
      const cur = usageMetersEl.dataset.variant === "rings" ? "rings" : "bars";
      usageMetersEl.dataset.variant = cur === "bars" ? "rings" : "bars";
      renderUsage(lastUsagePayload);  // re-render with the same data
    });
  }
  if (usageCreditsToggleEl) {
    usageCreditsToggleEl.addEventListener("click", () => {
      showCredits = !showCredits;
      try { localStorage.setItem("apiary.usage.showCredits", showCredits ? "1" : "0"); }
      catch (_) {}
      reflectCreditsToggle();
      renderUsage(lastUsagePayload);
    });
  }

  function renderUsage(payload) {
    if (payload !== null && payload !== undefined) {
      lastUsagePayload = payload;
      usageStale = false;
    } else {
      usageStale = true;
    }
    usageMetersEl.classList.toggle("stale", usageStale);
    if (lastUsagePayload === null) {
      usageBodyEl.textContent = "—";
      return;
    }
    const variant = usageMetersEl.dataset.variant === "rings" ? "rings" : "bars";
    const buckets = bucketsFromPayload(lastUsagePayload);
    usageBodyEl.innerHTML = "";
    if (variant === "rings") {
      usageBodyEl.appendChild(renderUsageRings(buckets));
    } else {
      usageBodyEl.appendChild(renderUsageBars(buckets));
    }
  }

  function bucketsFromPayload(p) {
    const rows = [
      { key: "5h", label: "5-hour", bucket: p.five_hour },
      { key: "7d", label: "7-day",  bucket: p.seven_day },
    ];
    const extra = p.extra_usage;
    if (extra && extra.is_enabled && showCredits) {
      const used = (extra.used_credits || 0) / 100;
      const limit = (extra.monthly_limit || 0) / 100;
      const cur = extra.currency || "USD";
      rows.push({
        key: "credits",
        label: "credits",
        bucket: {
          utilization: extra.utilization || 0,
          resets_at: null,
          sublabel: `${formatMoney(used, cur)} / ${formatMoney(limit, cur)}`,
        },
      });
    }
    return rows;
  }

  function formatMoney(amount, currency) {
    const sign = currency === "USD" ? "$" : "";
    return sign + amount.toFixed(2);
  }

  // "4h 52m", "2d 3h", "<1m", or "expired" for negative deltas.
  function formatResetIn(isoTs) {
    if (!isoTs) return "";
    const target = Date.parse(isoTs);
    if (!target) return "";
    const delta = target - Date.now();
    if (delta <= 0) return "reset due";
    const m = Math.floor(delta / 60000);
    if (m < 1) return "<1m";
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    const remM = m % 60;
    if (h < 24) return remM ? `${h}h ${remM}m` : `${h}h`;
    const d = Math.floor(h / 24);
    const remH = h % 24;
    return remH ? `${d}d ${remH}h` : `${d}d`;
  }

  function thresholdClass(pct, timePct) {
    // When a time-elapsed reference is available, the main indicator is
    // binary: "hot" when consumption is outrunning the clock (pct greater
    // than the red bar's pct), "cool" otherwise. Ties resolve to cool —
    // being exactly on pace is not a warning state.
    //
    // Fallback (no time reference — e.g. the credits bucket) preserves the
    // original absolute thresholds: cool / warm / hot at 50% / 80%.
    if (typeof timePct === "number") {
      return pct > timePct ? "warm" : "cool";
    }
    if (pct >= 80) return "hot";
    if (pct >= 50) return "warm";
    return "cool";
  }

  // Nominal reset-window length in ms, keyed by bucket.key. Drives the thin
  // secondary indicator that tracks how far into the current window we are.
  // Credits bucket has no time-reset cadence and is intentionally omitted.
  const USAGE_WINDOW_MS = {
    "5h": 5 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
  };

  // Percent of the reset window that has elapsed. Returns a number in [0, 100]
  // or null when it cannot be computed (missing or unparseable timestamp,
  // unknown bucket key). Null triggers the caller to skip rendering the
  // secondary indicator rather than show a stale/guessed value.
  function elapsedWindowPct(isoTs, windowMs) {
    if (!isoTs || !windowMs) return null;
    const target = Date.parse(isoTs);
    if (!target) return null;
    const remaining = target - Date.now();
    const elapsed = windowMs - remaining;
    const pct = (elapsed / windowMs) * 100;
    return Math.max(0, Math.min(100, pct));
  }

  // Variant 1 — stacked horizontal bars.
  function renderUsageBars(rows) {
    const wrap = document.createElement("div");
    wrap.className = "usage-bars";
    for (const row of rows) {
      const pct = Math.max(0, Math.min(100, Number(row.bucket.utilization) || 0));
      const timePct = elapsedWindowPct(row.bucket.resets_at, USAGE_WINDOW_MS[row.key]);
      const item = document.createElement("div");
      item.className = "usage-bar-row";
      const title = document.createElement("div");
      title.className = "usage-bar-title";
      const left = document.createElement("span");
      left.textContent = row.label;
      const right = document.createElement("span");
      right.className = "usage-bar-pct";
      right.textContent = `${pct.toFixed(0)}%`;
      title.appendChild(left);
      title.appendChild(right);
      item.appendChild(title);
      const track = document.createElement("div");
      track.className = `usage-bar-track ${thresholdClass(pct, timePct)}`;
      const fill = document.createElement("div");
      fill.className = "usage-bar-fill";
      fill.style.width = `${pct}%`;
      track.appendChild(fill);
      item.appendChild(track);
      if (timePct !== null) {
        const timeTrack = document.createElement("div");
        timeTrack.className = "usage-bar-time";
        const timeFill = document.createElement("div");
        timeFill.className = "usage-bar-time-fill";
        timeFill.style.width = `${timePct}%`;
        timeTrack.appendChild(timeFill);
        item.appendChild(timeTrack);
      }
      const foot = document.createElement("div");
      foot.className = "usage-bar-foot";
      const resetsIn = formatResetIn(row.bucket.resets_at);
      foot.textContent = row.bucket.sublabel
        || (resetsIn ? `resets in ${resetsIn}` : "");
      item.appendChild(foot);
      wrap.appendChild(item);
    }
    return wrap;
  }

  // Variant 2 — side-by-side SVG donut rings.
  const RING_R = 18;
  const RING_CIRC = 2 * Math.PI * RING_R;
  function renderUsageRings(rows) {
    const wrap = document.createElement("div");
    wrap.className = "usage-rings";
    for (const row of rows) {
      const pct = Math.max(0, Math.min(100, Number(row.bucket.utilization) || 0));
      const timePct = elapsedWindowPct(row.bucket.resets_at, USAGE_WINDOW_MS[row.key]);
      const cell = document.createElement("div");
      cell.className = `usage-ring-cell ${thresholdClass(pct, timePct)}`;
      const resetsIn = formatResetIn(row.bucket.resets_at);
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 44 44");
      svg.setAttribute("class", "usage-ring-svg");
      const track = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      track.setAttribute("class", "usage-ring-track");
      track.setAttribute("cx", "22"); track.setAttribute("cy", "22"); track.setAttribute("r", String(RING_R));
      svg.appendChild(track);
      const fill = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      fill.setAttribute("class", "usage-ring-fill");
      fill.setAttribute("cx", "22"); fill.setAttribute("cy", "22"); fill.setAttribute("r", String(RING_R));
      fill.setAttribute("stroke-dasharray", String(RING_CIRC));
      fill.setAttribute("stroke-dashoffset", String(RING_CIRC * (1 - pct / 100)));
      fill.setAttribute("transform", "rotate(-90 22 22)");
      svg.appendChild(fill);
      if (timePct !== null) {
        const TIME_R = 21;
        const TIME_CIRC = 2 * Math.PI * TIME_R;
        const timeTrack = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        timeTrack.setAttribute("class", "usage-ring-time-track");
        timeTrack.setAttribute("cx", "22"); timeTrack.setAttribute("cy", "22"); timeTrack.setAttribute("r", String(TIME_R));
        svg.appendChild(timeTrack);
        const timeFill = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        timeFill.setAttribute("class", "usage-ring-time-fill");
        timeFill.setAttribute("cx", "22"); timeFill.setAttribute("cy", "22"); timeFill.setAttribute("r", String(TIME_R));
        timeFill.setAttribute("stroke-dasharray", String(TIME_CIRC));
        timeFill.setAttribute("stroke-dashoffset", String(TIME_CIRC * (1 - timePct / 100)));
        timeFill.setAttribute("transform", "rotate(-90 22 22)");
        svg.appendChild(timeFill);
      }
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("class", "usage-ring-text");
      text.setAttribute("x", "22"); text.setAttribute("y", "22");
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("dominant-baseline", "central");
      text.textContent = `${pct.toFixed(0)}%`;
      svg.appendChild(text);
      cell.appendChild(svg);
      const label = document.createElement("div");
      label.className = "usage-ring-label";
      label.textContent = row.label;
      cell.appendChild(label);
      const sub = document.createElement("div");
      sub.className = "usage-ring-sub";
      sub.textContent = row.bucket.sublabel || (resetsIn ? `in ${resetsIn}` : "");
      cell.appendChild(sub);
      wrap.appendChild(cell);
    }
    return wrap;
  }

  // Re-tick every 30s so the "resets in Nh Mm" countdown stays current even
  // between backend pushes.
  setInterval(() => { if (lastUsagePayload) renderUsage(lastUsagePayload); }, 30000);

  async function loadNoteBody(note, bodyEl) {
    if (!note.body_path || !bridgeReady()) {
      bodyEl.textContent = note.summary || "(no body)";
      return;
    }
    bodyEl.textContent = "loading…";
    try {
      const body = await window.pywebview.api.get_note_body(note.body_path);
      const text = body || "(empty)";
      bodyEl.textContent = text;
      bodyEl.dataset.loaded = "1";
      bodyCache.set(note.display_id, text);
    } catch (e) {
      bodyEl.textContent = `(failed to read body: ${e})`;
    }
  }

  function toggleNote(item, note, bodyEl) {
    if (item.classList.contains("expanded")) {
      item.classList.remove("expanded");
      expandedNotes.delete(note.display_id);
      return;
    }
    item.classList.add("expanded");
    expandedNotes.add(note.display_id);
    if (!note.has_body) {
      const text = note.summary || "(no body)";
      bodyEl.textContent = text;
      bodyCache.set(note.display_id, text);
      return;
    }
    if (bodyEl.dataset.loaded === "1") return;
    if (bodyCache.has(note.display_id)) {
      bodyEl.textContent = bodyCache.get(note.display_id);
      bodyEl.dataset.loaded = "1";
      return;
    }
    loadNoteBody(note, bodyEl);
  }
  sidebarSearchEl.addEventListener("input", (e) => {
    sidebarFilter = e.target.value;
    if (sidebarSearchTimer) clearTimeout(sidebarSearchTimer);
    sidebarSearchTimer = setTimeout(renderSidebar, 200);
  });

  // --- pty terminal (xterm.js) ---------------------------------------------
  // xterm.js renders the pty stream as a real terminal, so ANSI escapes, cursor
  // repaints, and spinner redraws work correctly instead of leaving visible
  // garbage like the previous "stripAnsi-then-show-last-N-lines" approach.
  //
  // After an assistant message lands, claude repaints its status line, spinner
  // cleanup, tool result boxes, footer hints, etc. Suppress unread counting
  // during this window so trailing repaints don't fake an "awaiting input" signal.
  const PTY_POST_MSG_GRACE_MS = 3000;
  let unreadCount = 0;
  let lastPtyChunkAt = 0;
  let lastAsstMsgAt = 0;

  // Read back CSS vars for xterm theme so a theme.json change propagates.
  function themeVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  if (typeof window.Terminal !== "function") {
    console.error("[apiary-gui] xterm.js failed to load (window.Terminal undefined).");
  }
  if (typeof window.FitAddon !== "object" || !window.FitAddon.FitAddon) {
    console.error("[apiary-gui] xterm FitAddon failed to load (window.FitAddon.FitAddon missing).");
  }

  const term = new window.Terminal({
    fontFamily: themeVar("--font-family", "Consolas, monospace"),
    fontSize: 13,
    cursorBlink: true,
    scrollback: 5000,
    convertEol: false,
    cols: 120,
    rows: 30,
    theme: {
      background: themeVar("--pty-bg", "#010409"),
      foreground: themeVar("--pty-fg", "#c9d1d9"),
    },
  });
  const fitAddon = new window.FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  // Defer term.open() until the terminal first has real size — opening on a
  // zero-height container leaves xterm's renderer un-initialized. Writes to
  // term BEFORE open still update the internal buffer (xterm parses them),
  // so the prompt detector can read the buffer the whole time even while the
  // terminal is visually collapsed.
  let termOpened = false;

  // ConPTY bracketed-paste: pywinpty wraps multi-byte writes in paste markers,
  // inside which "\r" is a literal newline rather than a submit. Route Enter
  // (CR) and Esc through the dedicated control paths to avoid that wrapping;
  // everything else (printable keys, arrows, Tab, Ctrl+*) goes raw via send_text.
  term.onData((data) => {
    if (!bridgeReady()) return;
    if (data === "\r") {
      window.pywebview.api.send_control("m");
      return;
    }
    if (data === "\x1b") {
      window.pywebview.api.send_escape();
      return;
    }
    window.pywebview.api.send_text(data);
  });

  // Refit and notify the backend of new rows/cols. Only meaningful while the
  // terminal is visible — a collapsed (height:0) container yields bogus sizes.
  function refitTerminal() {
    if (ptyStripWrapEl.classList.contains("collapsed")) return;
    try {
      fitAddon.fit();
    } catch (_) {
      return;
    }
    if (bridgeReady() && typeof window.pywebview.api.pty_resize === "function") {
      window.pywebview.api.pty_resize(term.rows, term.cols);
    }
  }
  window.addEventListener("resize", refitTerminal);

  function ensureTermOpen() {
    if (termOpened) return;
    // Open immediately, even into a 0-height collapsed container. The renderer
    // can't lay out yet, but xterm only updates buffer.active reliably AFTER
    // open() — empirically, write()-before-open leaves buffer.active stale
    // (despite xterm docs implying otherwise), which made the prompt detector
    // miss prompts that fired while the pty pane was collapsed. fit() runs
    // later via ResizeObserver once the container actually has size.
    term.open(ptyTermEl);
    termOpened = true;
    try { fitAddon.fit(); } catch (_) {}
    if (bridgeReady() && typeof window.pywebview.api.pty_resize === "function") {
      window.pywebview.api.pty_resize(term.rows, term.cols);
    }
  }
  // Fallback trigger: once the terminal's container actually has size, open.
  // This is the robust path — it doesn't care whether the size came from a
  // manual toggle, auto-expand, or the user resizing the window. Also keeps
  // calling fit() after open so that the CSS height transition (0 → 280px)
  // doesn't leave xterm stuck at the mid-transition row count.
  try {
    const ro = new ResizeObserver(() => {
      const h = ptyTermEl.clientHeight;
      if (!termOpened && h >= 10) {
        ensureTermOpen();
      } else if (termOpened && h >= 10) {
        try { fitAddon.fit(); } catch (_) {}
        if (bridgeReady() && typeof window.pywebview.api.pty_resize === "function") {
          window.pywebview.api.pty_resize(term.rows, term.cols);
        }
      }
    });
    ro.observe(ptyTermEl);
  } catch (_) { /* ResizeObserver unavailable — toggle path still works */ }

  // Open immediately so the buffer starts populating from the first pty byte,
  // even if the user never expands the terminal pane.
  ensureTermOpen();

  function ptyExpand() {
    ptyStripWrapEl.classList.remove("collapsed");
    unreadCount = 0;
    ptyUnreadEl.classList.add("hidden");
    ptyUnreadEl.textContent = "";
    // ResizeObserver will catch the height change and call ensureTermOpen. As
    // a safety net, retry a few times across the CSS transition window too.
    const retryUntil = Date.now() + 400;
    function tryOpen() {
      ensureTermOpen();
      if (!termOpened && Date.now() < retryUntil) requestAnimationFrame(tryOpen);
      else refitTerminal();
    }
    requestAnimationFrame(tryOpen);
  }
  function ptyCollapse() {
    ptyStripWrapEl.classList.add("collapsed");
  }
  function bumpUnread() {
    unreadCount += 1;
    ptyUnreadEl.classList.remove("hidden");
    ptyUnreadEl.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
  }

  function inPostMessageGrace() {
    return lastAsstMsgAt > 0 && Date.now() - lastAsstMsgAt < PTY_POST_MSG_GRACE_MS;
  }

  // --- thinking bubble ------------------------------------------------------
  // Three animated dots at the bottom of the chat while claude is working.
  // The turn stays "active" (waitingForAssistant=true) from the user-sent
  // Enter until either 15s of pty silence or a tab switch. Individual
  // assistant messages inside that window only remove the DOM bubble —
  // subsequent pty chunks in the same turn re-spawn it so long tool chains
  // don't look idle.
  const THINKING_IDLE_MS = 15000;
  let thinkingEl = null;
  let waitingForAssistant = false;

  function ensureThinkingBubble() {
    if (!waitingForAssistant) return;
    if (thinkingEl && thinkingEl.isConnected) return;
    thinkingEl = document.createElement("li");
    thinkingEl.className = "msg thinking";
    thinkingEl.innerHTML =
      '<div class="thinking-body">' +
        '<span class="dot"></span><span class="dot"></span><span class="dot"></span>' +
      '</div>';
    messagesEl.appendChild(thinkingEl);
    maybeScroll();
  }

  function hideThinkingBubble(endTurn) {
    if (endTurn) waitingForAssistant = false;
    if (thinkingEl) {
      thinkingEl.remove();
      thinkingEl = null;
    }
  }

  setInterval(() => {
    if (!waitingForAssistant) return;
    if (Date.now() - lastPtyChunkAt > THINKING_IDLE_MS) {
      hideThinkingBubble(true);
    }
  }, 1000);

  function appendPtyChunk(chunk) {
    // Always write — the buffer is valid pre-open, so the prompt detector can
    // read it even when the terminal is visually collapsed.
    term.write(chunk);
    scheduleDetect();
    ensureThinkingBubble();
    // Buffer is moving again → next time it goes stationary with an unparsed
    // prompt, the fallback should fire anew (not dedup against the stale
    // signature from the last stationary state).
    unknownPromptNotifiedSig = null;
    if (inPostMessageGrace()) return;
    lastPtyChunkAt = Date.now();
    if (ptyStripWrapEl.classList.contains("collapsed")) bumpUnread();
  }

  // Fallback auto-expand: if the pty has been stationary for a while AND the
  // buffer shows a numbered-option prompt that the detector *couldn't* parse
  // into a banner (unknown prompt type, new claude version, MCP OAuth, etc.),
  // reveal the terminal so the user isn't stuck. Active/streaming claude
  // doesn't trigger this — only "waiting" states do.
  const UNKNOWN_PROMPT_QUIET_MS = 2500;
  const UNKNOWN_PROMPT_CHECK_MS = 500;
  let unknownPromptNotifiedSig = null;
  setInterval(() => {
    if (!ptyStripWrapEl.classList.contains("collapsed")) return;
    if (activePrompt) return;               // banner is handling it
    if (lastPtyChunkAt === 0) return;        // nothing has happened yet
    if (Date.now() - lastPtyChunkAt < UNKNOWN_PROMPT_QUIET_MS) return; // still busy
    const lines = readScreenLines();
    // If the structured detector would *accept* this buffer, the detector
    // (with its dismiss-cooldown) is in charge — not the fallback. This
    // covers the common "user just answered, prompt text is still in the
    // buffer but we're in cooldown" case: previously the fallback fired even
    // though there was nothing the user needed to see.
    if (detectPrompt(lines)) return;
    // Now the only remaining case: numbered options are visible but the
    // structured parser rejected the shape. That's a genuinely unknown prompt
    // type → reveal the terminal.
    let hasNumbered = false;
    for (let i = lines.length - 1; i >= 0 && i >= lines.length - 20; i--) {
      if (/^\s*[❯>]\s*\d+\.\s+/.test(lines[i])) { hasNumbered = true; break; }
    }
    if (!hasNumbered) return;
    // Dedupe — fire exactly once per distinct stationary-with-prompt state.
    // Reset happens when appendPtyChunk runs (claude is doing things again).
    const sig = lines.slice(-10).join("\n");
    if (sig === unknownPromptNotifiedSig) return;
    unknownPromptNotifiedSig = sig;
    ptyExpand();
    toast("Claude is waiting on something the GUI couldn't parse — check the terminal.", "error");
  }, UNKNOWN_PROMPT_CHECK_MS);

  // Click on the bordered "terminal" label toggles the terminal panel.
  // The surrounding bar is reserved for drag-resize of the chat input
  // (see the gutter drag handler below) -- stop propagation so a click
  // on the label doesn't also start a drag on the bar.
  ptyToggleLabelEl.addEventListener("mousedown", (e) => e.stopPropagation());
  ptyToggleLabelEl.addEventListener("click", (e) => {
    e.stopPropagation();
    if (ptyStripWrapEl.classList.contains("collapsed")) ptyExpand();
    else ptyCollapse();
  });

  // --- prompt detector (reads xterm's screen buffer) -----------------------
  // Claude Code's interactive prompts (plan-mode approval, trust folder,
  // tool permission, etc.) share one visual shape:
  //
  //     <question text>
  //
  //     ❯ 1. <option text>
  //       2. <option text>
  //       3. <option text>
  //
  //     <optional footer with keyboard hints>
  //
  // We read xterm's post-render buffer (cursor movements already applied, so
  // no spinner garbage) and extract that structure. Match anywhere in the
  // buffer — prompts often live a few rows above the status line.
  // After the user answers or dismisses a prompt, claude takes ~300-800ms to
  // repaint the buffer (spinner frames, transition to feedback mode, etc.).
  // Suppress re-showing the same prompt during this window so the banner
  // doesn't immediately pop back for a prompt the user already dealt with.
  const DISMISS_COOLDOWN_MS = 4000;
  // Signatures the user has explicitly answered via a button click. Persists
  // for the session — once answered, the same prompt should never re-banner
  // even if claude leaves the rendered prompt in the xterm scrollback for
  // longer than DISMISS_COOLDOWN_MS. (Cancel-via-× still uses the cooldown,
  // since the user may genuinely want the prompt back.)
  const answeredSigs = new Set();
  let activePrompt = null;   // { question, options, signature }
  let awaitingFeedback = false;  // true while claude is in option-4 feedback mode
  let lastDismissedSig = null;
  let lastDismissedAt = 0;
  // Parser lives in prompt_detector.js so it can be Node-tested. If the bundle
  // failed to load, fall back to a no-op stub so the rest of the UI still works.
  const detectPrompt = (window.apiaryPromptDetector && window.apiaryPromptDetector.detectPrompt) || (() => null);

  const SCREEN_SCAN_ROWS = 200;
  function readScreenLines() {
    const buf = term.buffer.active;
    if (!buf) return [];
    const total = buf.length;
    const start = Math.max(0, total - SCREEN_SCAN_ROWS);
    const lines = [];
    for (let y = start; y < total; y++) {
      const line = buf.getLine(y);
      if (!line) continue;
      lines.push(line.translateToString(true).replace(/\s+$/, ""));
    }
    return lines;
  }

  function renderPromptBanner(prompt) {
    promptBannerEl.innerHTML = "";
    // Block chat input while a prompt is pending — otherwise the user's text
    // lands in whatever input field the prompt is still showing (e.g. plan
    // mode's "tell claude what to change" feedback box).
    inputEl.classList.add("input-blocked");
    inputEl.placeholder = "Claude is waiting on a response — pick an option, or click ✕ to cancel";
    const close = document.createElement("button");
    close.className = "prompt-dismiss";
    close.type = "button";
    close.textContent = "✕";
    close.title = "cancel the prompt (sends Esc to claude) and hide this banner";
    close.addEventListener("click", () => hidePromptBanner({ cancel: true }));
    promptBannerEl.appendChild(close);

    // Question only in the banner — the plan body / context lives in the
    // chat (recorded the moment the prompt was detected), so repeating it
    // here would be visual noise right above the input.
    if (prompt.question) {
      const q = document.createElement("div");
      q.className = "prompt-question";
      q.textContent = prompt.question;
      promptBannerEl.appendChild(q);
    }
    const row = document.createElement("div");
    row.className = "prompt-options";
    for (const opt of prompt.options) {
      const btn = document.createElement("button");
      btn.className = "prompt-option" + (opt.selected ? " selected" : "");
      btn.type = "button";
      btn.textContent = `${opt.number}. ${opt.text}`;
      btn.addEventListener("click", () => answerPrompt(opt.number, opt.text));
      row.appendChild(btn);
    }
    promptBannerEl.appendChild(row);
    promptBannerEl.classList.remove("hidden");
  }

  function hidePromptBanner({ cancel = false } = {}) {
    // When the user explicitly dismisses the banner, also send Esc to the pty
    // so the underlying prompt is actually cancelled — otherwise the user
    // starts typing in the chat and their text lands in whatever input field
    // the prompt is still showing.
    if (cancel && bridgeReady()) {
      try { window.pywebview.api.send_escape(); } catch (_) {}
    }
    if (activePrompt) {
      lastDismissedSig = activePrompt.signature;
      lastDismissedAt = Date.now();
    }
    promptBannerEl.classList.add("hidden");
    promptBannerEl.innerHTML = "";
    activePrompt = null;
    awaitingFeedback = false;
    inputEl.classList.remove("input-blocked");
    inputEl.placeholder = INPUT_PLACEHOLDER_DEFAULT;
  }

  function promptUuid(sig) {
    // Deterministic id per prompt signature so the "appeared" record and the
    // later resolution update target the same DOM element.
    return `prompt-${btoa(unescape(encodeURIComponent(sig))).replace(/[^A-Za-z0-9]/g, "").slice(0, 32)}`;
  }

  function recordPromptAppeared(prompt) {
    const uuid = promptUuid(prompt.signature);
    if (messagesEl.querySelector(`li[data-uuid="${uuid}"]`)) return;
    const parts = [];
    if (prompt.question) parts.push(`**${prompt.question}**`);
    if (prompt.context) parts.push("", prompt.context);
    appendMessage({
      uuid,
      role: "prompt",
      text: parts.join("\n"),
      timestamp: new Date().toISOString(),
    });
  }

  function recordPromptResolution(prompt, chosenOpt) {
    const uuid = promptUuid(prompt.signature);
    const existing = messagesEl.querySelector(`li[data-uuid="${uuid}"]`);
    if (existing) {
      // Append the choice to the existing record instead of creating a second
      // entry — keeps history as one block per prompt interaction.
      const body = existing.querySelector(".msg-body");
      if (body && !existing.dataset.resolved) {
        const choice = document.createElement("div");
        choice.className = "prompt-choice";
        choice.innerHTML = "→ chose: <strong></strong>";
        choice.querySelector("strong").textContent =
          `${chosenOpt.number}. ${chosenOpt.text}`;
        body.appendChild(choice);
        existing.dataset.resolved = "1";
      }
      return;
    }
    // Fallback — shouldn't normally fire, but if the "appeared" record never
    // went out for some reason, at least record the resolution.
    appendMessage({
      uuid: `${uuid}-resolved`,
      role: "prompt",
      text: `→ chose: **${chosenOpt.number}. ${chosenOpt.text}**`,
      timestamp: new Date().toISOString(),
    });
  }

  function answerPrompt(digit, optText = "") {
    if (!bridgeReady() || !activePrompt) return;
    const chosenOpt = activePrompt.options.find(o => o.number === digit)
                   || { number: digit, text: optText };
    recordPromptResolution(activePrompt, chosenOpt);
    // Feedback-entry options (plan mode's "4. Tell Claude what to change") put
    // claude into a text-input sub-mode. We send the digit to enter that
    // mode, skip the Enter, hide the banner, and unblock the chat input so
    // the user can type their feedback and submit it with Enter as normal.
    const isFeedback = /^\s*tell\b|what to change|with this feedback/i.test(optText);
    // Permanently mute this signature for the session — claude often leaves
    // the answered prompt rendered in the buffer past DISMISS_COOLDOWN_MS,
    // and we don't want the banner to re-appear after the user has already
    // committed to a choice.
    answeredSigs.add(activePrompt.signature);
    window.pywebview.api.send_text(String(digit));
    if (isFeedback) {
      awaitingFeedback = true;
      hidePromptBanner();
      inputEl.placeholder = "Type your feedback for claude — Enter to submit, Esc to cancel";
      inputEl.focus();
    } else {
      setTimeout(() => window.pywebview.api.send_control("m"), 30);
      hidePromptBanner();
    }
  }

  // Per-tab "allow Claude to edit its own settings" toggle (T-2026-176 #1):
  // when ON and the detected prompt is the harness protect-self gate for
  // .claude/ writes, auto-click option 1 ("Yes, and allow Claude to edit its
  // own settings for this session") without rendering the banner.
  function isSelfEditPrompt(prompt) {
    const hay = `${prompt.question || ""}\n${prompt.context || ""}\n` +
                (prompt.options || []).map(o => o.text || "").join("\n");
    return /\.claude[\\/]/.test(hay) || /edit its own settings/i.test(hay);
  }

  function autoAckSelfEdit(found) {
    const opt = (found.options || []).find(o => /^yes/i.test(o.text || "")) ||
                (found.options || [])[0];
    if (!opt) return false;
    answeredSigs.add(found.signature);
    recordPromptAppeared(found);
    recordPromptResolution(found, opt);
    try { window.pywebview.api.send_text(String(opt.number)); } catch (_) {}
    setTimeout(() => {
      try { window.pywebview.api.send_control("m"); } catch (_) {}
    }, 30);
    return true;
  }

  function runDetect() {
    const found = detectPrompt(readScreenLines());
    if (!found) {
      if (activePrompt) hidePromptBanner();
      // Also clear the dismiss cooldown — if the prompt is truly gone, a
      // later recurrence is a NEW prompt and deserves a banner.
      if (lastDismissedSig && (!activePrompt)) {
        lastDismissedSig = null;
        lastDismissedAt = 0;
      }
      return;
    }
    if (activePrompt && activePrompt.signature === found.signature) return;
    // Already-answered signatures stay muted for the rest of the session,
    // independent of the dismiss-cooldown window.
    if (answeredSigs.has(found.signature)) return;
    // Suppress re-show if the user just dismissed this exact prompt — claude
    // takes a moment to repaint, and the stale buffer would fire the banner
    // right back in the user's face.
    if (found.signature === lastDismissedSig &&
        Date.now() - lastDismissedAt < DISMISS_COOLDOWN_MS) {
      return;
    }
    const settings = getSessionSettings(activeSessionId);
    if (settings.allow_self_edits && isSelfEditPrompt(found)) {
      if (autoAckSelfEdit(found)) return;
    }
    activePrompt = found;
    recordPromptAppeared(found);
    renderPromptBanner(found);
  }

  // Steady-state poll instead of chunk-triggered debounce. Claude Code's
  // spinner repaints continuously while a prompt is waiting for input, so a
  // debounce keyed on appendPtyChunk never quiesces and the detector never
  // fires (T-2026-171, T-2026-172). A 500ms poll runs independently of chunk
  // churn; detectPrompt is cheap (regex over ~200 buffer rows).
  const PROMPT_DETECT_INTERVAL_MS = 500;
  setInterval(runDetect, PROMPT_DETECT_INTERVAL_MS);
  function scheduleDetect() { /* retained as hook point; poll now drives detection */ }

  // --- handoff banner (T-2026-164) ------------------------------------------
  // Shown on GUI start when core/startup.py reports unfilled handoffs. Button
  // types `/backfill-handoffs` into the composer (user presses Enter to submit).
  // Manual dismiss only — auto-hide was aggressive if the user alt-tabbed during
  // launch, and the × button is always available.
  function hideHandoffBanner() {
    handoffBannerEl.classList.add("hidden");
  }
  function showHandoffBanner(count) {
    const n = Number(count) || 0;
    if (n <= 0) { hideHandoffBanner(); return; }
    handoffBannerTextEl.textContent =
      `${n} previous session${n === 1 ? "" : "s"} not yet summarized.`;
    handoffBannerEl.classList.remove("hidden");
  }
  handoffBannerBtnEl.addEventListener("click", () => {
    inputEl.value = "/backfill-handoffs";
    inputEl.focus();
    hideHandoffBanner();
  });
  handoffBannerDismissEl.addEventListener("click", hideHandoffBanner);

  // --- bridge surface (Python → JS) ----------------------------------------
  function bridgeReady() {
    return typeof window.pywebview !== "undefined" && window.pywebview.api;
  }

  // Tab-aware routing: each push from the backend carries the session_id
  // it came from. Non-active session pushes are dropped on the floor for
  // now (Phase 3 will buffer them per-tab). session_id is empty for app-
  // global pushes (theme, notes) — those always apply.
  let activeSessionId = "";
  function pushIsForActive(sid) {
    if (!sid) return true;          // app-global
    if (!activeSessionId) return true;  // no active yet — accept
    return sid === activeSessionId;
  }

  window.apiary = {
    setActiveSession(sid) {
      const nextSid = String(sid || "");
      if (nextSid === activeSessionId) return;
      const isFirstActivation = !activeSessionId;
      activeSessionId = nextSid;
      // On a real tab SWITCH we reset UI state so the outgoing tab's messages,
      // xterm scrollback, prompt banner, and sidebar don't leak into the new
      // tab. On the FIRST activation (app startup, empty "" → real sid), we
      // skip the reset because the backend's discovery thread might have
      // already pushed the active session's history before setActiveSession
      // arrives — clearing would wipe that history.
      if (isFirstActivation) return;
      try { clearMessages(); } catch (_) {}
      try { term.reset(); } catch (_) {}
      if (typeof hideThinkingBubble === "function") hideThinkingBubble(true);
      if (typeof hidePromptBanner === "function") hidePromptBanner();
      try { allNotes = []; renderSidebar(); } catch (_) {}
    },
    onMessage(msgJson, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      try {
        const msg = typeof msgJson === "string" ? JSON.parse(msgJson) : msgJson;
        appendMessage(msg);
      } catch (e) {
        console.error("onMessage parse error", e);
      }
    },
    onMessages(arrJson, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      try {
        const arr = typeof arrJson === "string" ? JSON.parse(arrJson) : arrJson;
        if (Array.isArray(arr)) for (const m of arr) appendMessage(m);
      } catch (e) {
        console.error("onMessages parse error", e);
      }
    },
    onClear(sessionId) {
      if (!pushIsForActive(sessionId)) return;
      clearMessages();
    },
    onStatus(text, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      setStatus(text || "");
    },
    onToast(text, kind, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      toast(text, kind);
    },
    onPtyChunk(text, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      try {
        appendPtyChunk(String(text || ""));
      } catch (e) {
        console.error("onPtyChunk error", e);
      }
    },
    onPtyExit(code, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      toast(`Claude Code exited (code ${code}) — restart from menu`, "error");
    },
    onSessions(arrJson) {
      // Phase 2: data only. Phase 3 renders the tab bar.
      try {
        const arr = typeof arrJson === "string" ? JSON.parse(arrJson) : arrJson;
        if (Array.isArray(arr)) {
          window.apiary.__sessions = arr;
          if (typeof window.renderTabs === "function") window.renderTabs(arr);
        }
      } catch (e) {
        console.error("onSessions parse error", e);
      }
    },
    onNotes(arrJson, sessionId) {
      if (!pushIsForActive(sessionId)) return;
      try {
        const arr = typeof arrJson === "string" ? JSON.parse(arrJson) : arrJson;
        if (Array.isArray(arr)) {
          allNotes = arr;
          renderSidebar();
        }
      } catch (e) {
        console.error("onNotes parse error", e);
      }
    },
    onTheme(varsJson) {
      try {
        const vars = typeof varsJson === "string" ? JSON.parse(varsJson) : varsJson;
        if (vars && typeof vars === "object") {
          for (const [k, v] of Object.entries(vars)) {
            const name = k.startsWith("--") ? k : `--${k}`;
            document.documentElement.style.setProperty(name, v);
          }
        }
        // Re-apply pty colors to xterm (CSS vars alone don't reach the canvas).
        try {
          term.options.theme = {
            background: themeVar("--pty-bg", "#010409"),
            foreground: themeVar("--pty-fg", "#c9d1d9"),
          };
        } catch (_) { /* noop */ }
      } catch (e) {
        console.error("onTheme parse error", e);
      }
    },
    onHandoffBanner(count) {
      try { showHandoffBanner(count); } catch (e) { console.error("onHandoffBanner error", e); }
    },
    onUsage(payload) {
      try { renderUsage(payload); } catch (e) { console.error("onUsage error", e); }
    },
  };

  // --- dev: Ctrl+R / F5 reloads the page (frontend hot-reload) --------------
  // WebView2 swallows the default reload shortcuts, so we wire them ourselves.
  // Reloads re-fetch index.html / app.css / app.js; the Python backend keeps
  // running, so tabs and pty subprocesses survive. Useful when iterating on
  // gui/web/* without restarting the whole GUI.
  document.addEventListener("keydown", (e) => {
    const isReload =
      e.key === "F5" ||
      ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "r");
    if (isReload) {
      e.preventDefault();
      window.location.reload();
    }
  });

  // --- input wiring ---------------------------------------------------------
  inputEl.addEventListener("keydown", (e) => {
    if (!bridgeReady()) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      // While a prompt banner is visible, forwarding the chat text would land
      // it inside whatever input field the pty prompt is showing (plan mode's
      // feedback field, etc.). Block the send and nudge the user.
      if (activePrompt) {
        toast("Pick an option in the banner above (or click ✕ to cancel the prompt).", "error");
        return;
      }
      const text = inputEl.value;
      inputEl.value = "";
      // Render optimistically — the tail will reconcile when the real record
      // lands. Skip empties and slash commands (slash commands shouldn't show
      // as user messages; claude often handles them differently). Also skip
      // the tentative render while we're in feedback-submission mode —
      // claude treats this as input to the plan prompt, not a new user msg.
      if (text && !text.startsWith("/") && !awaitingFeedback) {
        if (!waitingForAssistant) {
          // Fresh turn (not queued). Clear any stale .queued markers from
          // prior turns: if claude bundled multiple queued messages into one
          // reply (so not every queued msg got a dedicated response), those
          // stragglers still carry the class. Left uncleared, the next
          // assistant reply would wrongly insert before them.
          messagesEl.querySelectorAll("li.msg.user.queued")
            .forEach(el => el.classList.remove("queued"));
        }
        // If waitingForAssistant is already true, claude is still working on
        // a prior turn — this message is queued, and needs the .queued marker
        // so the assistant reply for the prior turn lands before it.
        appendTentativeUserMessage(text, waitingForAssistant);
        // From here until the assistant message lands, the thinking bubble
        // may spawn on pty chunks. Slash commands and feedback submissions
        // don't get a normal assistant reply, so we don't arm for them.
        waitingForAssistant = true;
      }
      window.pywebview.api.send_input(text);
      if (awaitingFeedback) {
        awaitingFeedback = false;
        inputEl.placeholder = INPUT_PLACEHOLDER_DEFAULT;
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      window.pywebview.api.send_escape();
      // ESC cancels claude-code's current turn, but the text from the most
      // recent Enter can linger in its input prompt. Without clearing it,
      // the user's next message gets concatenated onto the leftover text.
      // Send Ctrl+U (readline kill-line) a moment later — long enough for
      // claude-code to finish handling the interrupt and return to prompt.
      setTimeout(() => {
        try { window.pywebview.api.send_control("u"); } catch (_) {}
      }, 150);
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c") {
      const sel = inputEl.value.substring(inputEl.selectionStart, inputEl.selectionEnd);
      if (!sel) {
        e.preventDefault();
        window.pywebview.api.send_control("c");
      }
    }
  });

  // --- initial state --------------------------------------------------------
  setStatus("Waiting for session…");
  refreshTotalsBadges();

  // Hydrate persisted sidebar collapsed state once the bridge is ready.
  function hydrateCollapsed() {
    if (!bridgeReady()) {
      setTimeout(hydrateCollapsed, 100);
      return;
    }
    Promise.resolve(window.pywebview.api.get_sidebar_collapsed())
      .then((arr) => {
        if (Array.isArray(arr)) {
          collapsed = new Set(arr);
          renderSidebar();
        }
      })
      .catch((e) => console.error("get_sidebar_collapsed failed", e));
  }
  hydrateCollapsed();

  // --- chat-input resize via the pty-toggle bar ----------------------------
  // The bar above the composer (showing the "terminal" label + chevron)
  // doubles as the chat-input resize handle (T-2026-157). Drag anywhere on
  // the bar (except the bordered "terminal" label, which toggles the
  // terminal panel) to resize the textarea up/down. Persisted height is
  // applied to the textarea itself; the composer footer grows around it.
  const COMPOSER_MIN = 24;
  const COMPOSER_MAX_FRAC = 0.7;  // never let the input eat more than 70% of viewport

  function clampHeight(px) {
    const max = Math.max(COMPOSER_MIN, Math.floor(window.innerHeight * COMPOSER_MAX_FRAC));
    return Math.max(COMPOSER_MIN, Math.min(max, px));
  }

  function applyComposerHeight(px) {
    const h = clampHeight(px);
    inputEl.style.height = h + "px";
  }

  function hydrateComposerHeight() {
    if (!bridgeReady()) {
      setTimeout(hydrateComposerHeight, 100);
      return;
    }
    Promise.resolve(window.pywebview.api.get_composer_height())
      .then((px) => {
        if (typeof px === "number" && px > 0) applyComposerHeight(px);
      })
      .catch((e) => console.error("get_composer_height failed", e));
  }
  hydrateComposerHeight();

  let dragState = null;
  ptyToggleEl.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    dragState = { startY: e.clientY, startHeight: inputEl.offsetHeight };
    ptyToggleEl.classList.add("dragging");
    document.body.style.cursor = "ns-resize";
  });

  document.addEventListener("mousemove", (e) => {
    if (!dragState) return;
    // Drag up (clientY decreases) -> taller input. Subtract delta so up-drag
    // gives a positive growth.
    const delta = dragState.startY - e.clientY;
    applyComposerHeight(dragState.startHeight + delta);
  });

  document.addEventListener("mouseup", () => {
    if (!dragState) return;
    const finalHeight = inputEl.offsetHeight;
    dragState = null;
    ptyToggleEl.classList.remove("dragging");
    document.body.style.cursor = "";
    if (bridgeReady()) {
      Promise.resolve(window.pywebview.api.save_composer_height(finalHeight))
        .catch((e) => console.error("save_composer_height failed", e));
    }
  });
})();
