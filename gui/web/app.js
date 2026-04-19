// apiary GUI frontend.

(() => {
  // --- DOM handles ----------------------------------------------------------
  const messagesEl = document.getElementById("messages");
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
  const ptyUnreadEl = document.getElementById("pty-unread");
  const promptBannerEl = document.getElementById("prompt-banner");
  const inputEl = document.getElementById("input");
  const INPUT_PLACEHOLDER_DEFAULT = inputEl.getAttribute("placeholder") || "";
  const sidebarSearchEl = document.getElementById("sidebar-search");
  const sidebarListEl = document.getElementById("sidebar-list");

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
  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
  // Track whether the user has scrolled away — once they have, stay sticky=false
  // until they manually return to bottom.
  messagesEl.addEventListener("scroll", () => {
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
    if (msg.role === "user" && msg.text) {
      const tentatives = messagesEl.querySelectorAll("li.msg.user.tentative");
      for (const el of tentatives) {
        if (el.dataset.text === msg.text) {
          insertAnchor = el.nextSibling;
          el.remove();
          break;
        }
      }
    }

    const li = document.createElement("li");
    li.className = `msg ${msg.role}`;
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
      // The real message is now in the chat — drop the transient dots above
      // it (if any). The idle timer would catch it too, but this makes the
      // transition visually clean the instant the message lands.
      if (typeof hideThinkingBubble === "function") hideThinkingBubble();
    }

    maybeScroll();
  }

  // Optimistic render: synthesize a user message and append immediately on send.
  // Marked .tentative so the eventual JSONL-derived record replaces it cleanly.
  function appendTentativeUserMessage(text) {
    const li = document.createElement("li");
    li.className = "msg user tentative";
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
        item.appendChild(idEl);

        const sumEl = document.createElement("div");
        sumEl.className = "sidebar-item-summary";
        sumEl.textContent = n.summary || "(no summary)";
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
    if (ptyTermEl.clientHeight < 10) {
      // Container still collapsed / mid-transition. The ResizeObserver below
      // will call us again once size settles.
      return;
    }
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

  // --- thinking bubble + pty rate indicator ---------------------------------
  // While the pty is streaming chunks, show a transient bubble at the bottom
  // of the chat with three animated dots and the current throughput. Hides on
  // idle (>THINKING_IDLE_MS since last chunk) or when a real assistant message
  // arrives via JSONL (whichever comes first).
  const THINKING_IDLE_MS = 600;
  const RATE_WINDOW_MS = 1000;
  const rateWindow = []; // [{t: ms, b: bytes}, ...]
  let thinkingEl = null;

  function recordChunkForRate(size) {
    const now = Date.now();
    rateWindow.push({ t: now, b: size });
    while (rateWindow.length && now - rateWindow[0].t > RATE_WINDOW_MS) {
      rateWindow.shift();
    }
  }

  function currentRate() {
    if (rateWindow.length === 0) return 0;
    const now = Date.now();
    const span = Math.max(now - rateWindow[0].t, 100);
    let total = 0;
    for (const e of rateWindow) total += e.b;
    return (total * 1000) / span;
  }

  function fmtRate(bps) {
    if (bps < 1024) return `${Math.round(bps)} B/s`;
    if (bps < 1024 * 1024) return `${(bps / 1024).toFixed(1)} KB/s`;
    return `${(bps / 1024 / 1024).toFixed(2)} MB/s`;
  }

  function ensureThinkingBubble() {
    if (thinkingEl && thinkingEl.isConnected) return;
    thinkingEl = document.createElement("li");
    thinkingEl.className = "msg thinking";
    thinkingEl.innerHTML =
      '<div class="thinking-body">' +
        '<span class="dot"></span><span class="dot"></span><span class="dot"></span>' +
        '<span class="thinking-rate"></span>' +
      '</div>';
    messagesEl.appendChild(thinkingEl);
    maybeScroll();
  }

  function hideThinkingBubble() {
    if (thinkingEl) {
      thinkingEl.remove();
      thinkingEl = null;
    }
  }

  setInterval(() => {
    if (!thinkingEl) return;
    if (Date.now() - lastPtyChunkAt > THINKING_IDLE_MS) {
      hideThinkingBubble();
      return;
    }
    const rateEl = thinkingEl.querySelector(".thinking-rate");
    if (rateEl) rateEl.textContent = fmtRate(currentRate());
  }, 200);

  function appendPtyChunk(chunk) {
    // Always write — the buffer is valid pre-open, so the prompt detector can
    // read it even when the terminal is visually collapsed.
    term.write(chunk);
    scheduleDetect();
    recordChunkForRate(chunk.length || 0);
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
      if (/^\s*❯\s*\d+\.\s+/.test(lines[i])) { hasNumbered = true; break; }
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

  ptyToggleEl.addEventListener("click", () => {
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
  const PROMPT_DETECT_DEBOUNCE_MS = 250;
  const PROMPT_MAX_OPTIONS = 9;  // claude uses 1-digit numbering
  const PROMPT_SCAN_BACK_LINES = 6;
  // After the user answers or dismisses a prompt, claude takes ~300-800ms to
  // repaint the buffer (spinner frames, transition to feedback mode, etc.).
  // Suppress re-showing the same prompt during this window so the banner
  // doesn't immediately pop back for a prompt the user already dealt with.
  const DISMISS_COOLDOWN_MS = 4000;
  let promptDetectTimer = null;
  let activePrompt = null;   // { question, options, signature }
  let awaitingFeedback = false;  // true while claude is in option-4 feedback mode
  let lastDismissedSig = null;
  let lastDismissedAt = 0;

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

  function detectPrompt(lines) {
    // Find a selected-option line: "❯ N. ..." possibly indented.
    for (let i = lines.length - 1; i >= 0; i--) {
      const sel = lines[i].match(/^\s*❯\s*(\d+)\.\s+(.+)$/);
      if (!sel) continue;
      const firstNum = parseInt(sel[1], 10);
      const options = [{
        number: firstNum,
        text: sel[2].trim(),
        selected: true,
      }];
      // Forward-scan: additional numbered options, allowing continuation
      // lines (indented without a number) attached to the previous option.
      let expected = firstNum + 1;
      for (let j = i + 1; j < lines.length && options.length < PROMPT_MAX_OPTIONS; j++) {
        const m = lines[j].match(/^\s+(\d+)\.\s+(.+)$/);
        if (m && parseInt(m[1], 10) === expected) {
          options.push({ number: expected, text: m[2].trim(), selected: false });
          expected += 1;
          continue;
        }
        if (m) break;  // number out of order — end of options
        if (lines[j].trim() === "") continue;  // blank tolerated inside options
        // Non-numbered, non-blank → options section ended.
        break;
      }
      if (options.length < 2) continue;  // need at least 2 to be a choice
      // Backward-scan: last non-empty line before options = question.
      let question = "";
      let questionIdx = -1;
      for (let k = i - 1; k >= 0 && k >= i - PROMPT_SCAN_BACK_LINES; k--) {
        const t = lines[k].trim();
        if (t) { question = t; questionIdx = k; break; }
      }
      // Extract context text above the prompt. Plan mode frames its body with
      // a ╌-divider (U+254C) separating the plan from the question. The top
      // of the plan is bounded either by a second ╌-divider (long plans) or
      // by Claude Code's status chrome — status-line ─-dividers (U+2500) or
      // mode indicators like "⏸ plan mode on". Scan accordingly.
      let context = "";
      if (questionIdx > 0) {
        const PLAN_DIV = /^\s*╌{10,}\s*$/;
        const CHROME_LINE = /^\s*─{10,}\s*$|^\s*[─❯⏸▐▛▜▟▝▘█]/;
        let bottomDiv = -1;
        for (let k = questionIdx - 1; k >= 0; k--) {
          if (PLAN_DIV.test(lines[k])) { bottomDiv = k; break; }
        }
        if (bottomDiv >= 0) {
          let topBound = 0;
          for (let k = bottomDiv - 1; k >= 0; k--) {
            if (PLAN_DIV.test(lines[k]) || CHROME_LINE.test(lines[k])) {
              topBound = k + 1;
              break;
            }
          }
          const body = lines.slice(topBound, bottomDiv)
                            .map(s => s.replace(/^\s{1,2}/, ""))
                            .join("\n")
                            .replace(/\n{3,}/g, "\n\n")
                            .trim();
          if (body) context = body;
        }
      }
      const signature = (context ? context.slice(0, 80) + "|" : "") +
                        options.map(o => `${o.number}.${o.text}`).join("|");
      return { question, context, options, signature };
    }
    return null;
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

  function scheduleDetect() {
    if (promptDetectTimer) clearTimeout(promptDetectTimer);
    promptDetectTimer = setTimeout(() => {
      promptDetectTimer = null;
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
      // Suppress re-show if the user just dismissed this exact prompt — claude
      // takes a moment to repaint, and the stale buffer would fire the banner
      // right back in the user's face.
      if (found.signature === lastDismissedSig &&
          Date.now() - lastDismissedAt < DISMISS_COOLDOWN_MS) {
        return;
      }
      activePrompt = found;
      recordPromptAppeared(found);
      renderPromptBanner(found);
    }, PROMPT_DETECT_DEBOUNCE_MS);
  }

  // --- bridge surface (Python → JS) ----------------------------------------
  function bridgeReady() {
    return typeof window.pywebview !== "undefined" && window.pywebview.api;
  }

  window.apiary = {
    onMessage(msgJson) {
      try {
        const msg = typeof msgJson === "string" ? JSON.parse(msgJson) : msgJson;
        appendMessage(msg);
      } catch (e) {
        console.error("onMessage parse error", e);
      }
    },
    onMessages(arrJson) {
      try {
        const arr = typeof arrJson === "string" ? JSON.parse(arrJson) : arrJson;
        if (Array.isArray(arr)) for (const m of arr) appendMessage(m);
      } catch (e) {
        console.error("onMessages parse error", e);
      }
    },
    onClear() {
      clearMessages();
    },
    onStatus(text) {
      setStatus(text || "");
    },
    onToast(text, kind) {
      toast(text, kind);
    },
    onPtyChunk(text) {
      try {
        appendPtyChunk(String(text || ""));
      } catch (e) {
        console.error("onPtyChunk error", e);
      }
    },
    onPtyExit(code) {
      toast(`Claude Code exited (code ${code}) — restart from menu`, "error");
    },
    onNotes(arrJson) {
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
  };

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
        appendTentativeUserMessage(text);
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
})();
