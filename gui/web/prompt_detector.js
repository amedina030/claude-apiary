// Claude Code interactive-prompt parser — extracted from app.js so it can be
// unit-tested in Node without a full browser / xterm.js context.
//
// The parser operates on a plain `string[]` (xterm's rendered-and-trimmed
// buffer rows). Producing that array from raw pty bytes needs cursor-movement
// emulation (xterm does that live in the browser). Tests feed hand-crafted
// line arrays that mirror the buffer shapes we've observed in the wild.
//
// Exposed in both environments:
//   - Browser: attaches to `window.apiaryPromptDetector`
//   - Node:    `module.exports = { detectPrompt, ... }`

(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.apiaryPromptDetector = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  const PROMPT_MAX_OPTIONS = 9;
  const PROMPT_SCAN_BACK_LINES = 6;
  // How far above a navigation footer we'll hunt for the menu's first option in
  // the glyph-less fallback pass.
  const FOOTER_LOOKBACK_LINES = 30;
  // claude prints this under arrow-navigable menus. It's the strongest
  // menu-specific signal we have, so it gates the looser detection paths
  // (per-option descriptions, and the glyph-less shape) against false positives.
  const NAV_HINT = /↑|↓|to select|to navigate|esc to cancel|enter to (?:select|confirm)/i;

  // Normalize the optional highlighted-row hint into a Set of line indices.
  // The browser derives this from xterm cell attributes (reverse-video /
  // non-default background — the colored bar AskUserQuestion paints behind the
  // selected row); Node tests pass an explicit array. Absent/empty → glyph-only
  // selection, preserving the pre-Phase-2 behavior.
  function toHighlightSet(highlighted) {
    if (highlighted instanceof Set) return highlighted;
    if (Array.isArray(highlighted)) return new Set(highlighted);
    return new Set();
  }

  // Parse a numbered-option block whose first (lowest-numbered) option sits at
  // line `i`. The selector glyph is optional: claude historically rendered ❯
  // (U+276F), 2.1.116+ emits a literal '>', and the AskUserQuestion card emits
  // NO glyph at all (the selected row is shown via xterm cell highlight, which
  // translateToString() discards — so we accept it out-of-band via `highlight`).
  // Returns {options, lastIdx, sawDescription} or null when fewer than two
  // options parse. Each option carries `lineIdx` (its buffer row) so the arrow
  // driver can verify the highlighted row after stepping.
  function parseOptionsFrom(lines, i, highlight) {
    const hl = toHighlightSet(highlight);
    // Capture the prefix (group 1) so we know the column the option number sits
    // at — description lines indent past it.
    const sel = lines[i].match(/^(\s*[❯>]?\s*)(\d+)\.\s+(.+)$/);
    if (!sel) return null;
    const firstNum = parseInt(sel[2], 10);
    const hasGlyph = /[❯>]/.test(sel[1]);
    const options = [{
      number: firstNum,
      text: sel[3].trim(),
      description: "",
      // Selection comes from the glyph (classic shapes) OR the cell-highlight
      // hint (glyph-less AskUserQuestion cards, cursor-off-first, >9 options).
      selected: hasGlyph || hl.has(i),
      indent: sel[1].length,
      lineIdx: i,
    }];
    let expected = firstNum + 1;
    let sawDescription = false;
    let lastIdx = i;
    for (let j = i + 1; j < lines.length && options.length < PROMPT_MAX_OPTIONS; j++) {
      lastIdx = j;
      const m = lines[j].match(/^(\s+)(\d+)\.\s+(.+)$/);
      if (m && parseInt(m[2], 10) === expected) {
        options.push({
          number: expected,
          text: m[3].trim(),
          description: "",
          selected: hl.has(j),
          indent: m[1].length,
          lineIdx: j,
        });
        expected += 1;
        continue;
      }
      if (m) break;                       // numbered, but out of sequence
      if (lines[j].trim() === "") continue;
      // Non-blank, non-numbered line. AskUserQuestion and other rich menus
      // render an indented description / wrapped sub-line under each option;
      // the old parser stopped dead here and lost the whole prompt. Attach it
      // to the current option when it indents past that option's number
      // column; anything at-or-left of the number (footer, chrome, ordinary
      // output) ends the option list.
      const lead = lines[j].match(/^(\s*)/)[1].length;
      const last = options[options.length - 1];
      if (lead > last.indent) {
        const extra = lines[j].trim();
        last.description = last.description ? last.description + " " + extra : extra;
        sawDescription = true;
        continue;
      }
      break;
    }
    if (options.length < 2) return null;
    return { options, lastIdx, sawDescription };
  }

  // Does any line in [from, to) carry the navigation footer?
  function hasNavFooter(lines, from, to) {
    for (let k = Math.max(0, from); k < Math.min(lines.length, to); k++) {
      if (NAV_HINT.test(lines[k])) return true;
    }
    return false;
  }

  // Locate the first option line of a glyph-less menu: when claude omits the
  // selector glyph entirely, the only reliable anchor is the navigation footer,
  // so we find it and walk up to the menu's "1." line. Returns the anchor index
  // or -1.
  function findGlyphlessAnchor(lines) {
    for (let f = lines.length - 1; f >= 0; f--) {
      if (!NAV_HINT.test(lines[f])) continue;
      for (let k = f - 1; k >= 0 && k >= f - FOOTER_LOOKBACK_LINES; k--) {
        if (/^\s*1\.\s+\S/.test(lines[k])) return k;
      }
    }
    return -1;
  }

  function buildResult(lines, i, parsed) {
    const { options, lastIdx } = parsed;
    let question = "";
    let questionIdx = -1;
    for (let k = i - 1; k >= 0 && k >= i - PROMPT_SCAN_BACK_LINES; k--) {
      const t = lines[k].trim();
      if (t) { question = t; questionIdx = k; break; }
    }
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
    void lastIdx;
    const signature = (context ? context.slice(0, 80) + "|" : "") +
                      options.map(o => `${o.number}.${o.text}`).join("|");
    return { question, context, options, signature };
  }

  // `opts.highlightedRows` (Set|array of line indices) is the optional
  // cell-attribute highlight hint from the browser; omitted in most tests.
  function detectPrompt(lines, opts) {
    const highlight = opts && opts.highlightedRows;
    // Pass 1 — glyphed anchor (❯ or '>'), scanned bottom-up. This is the
    // historical path; the glyph uniquely marks the selected (top) option, so
    // it's a safe anchor with no footer needed for classic shapes.
    for (let i = lines.length - 1; i >= 0; i--) {
      if (!/^\s*[❯>]\s*\d+\.\s+/.test(lines[i])) continue;
      const parsed = parseOptionsFrom(lines, i, highlight);
      if (!parsed) continue;
      // Guard the looser description path against false positives: a menu that
      // carries per-option descriptions must also show a navigation footer.
      // Classic description-less numbered prompts keep the old, footer-free
      // acceptance so plan-mode / trust-folder / permission shapes don't
      // regress.
      if (parsed.sawDescription &&
          !hasNavFooter(lines, i, parsed.lastIdx + 3)) {
        continue;
      }
      return buildResult(lines, i, parsed);
    }

    // Pass 2 — glyph-less menu (AskUserQuestion). No glyph means no per-row
    // anchor, so we key off the navigation footer (a strong menu-specific
    // signal) and walk up to the "1." line. Always footer-gated, so ordinary
    // numbered output can't masquerade as a menu.
    const anchor = findGlyphlessAnchor(lines);
    if (anchor >= 0) {
      const parsed = parseOptionsFrom(lines, anchor, highlight);
      // Proximity gate: the nav footer must sit immediately after the option
      // block. A real menu always prints its footer on the line that ends the
      // options (blank lines are skipped during parsing, so parsed.lastIdx
      // lands on that footer). The claude TUI renders the whole conversation —
      // including the assistant's own chat messages — into this same buffer, so
      // a stray "1./2./3." numbered list in prose would otherwise be anchored to
      // claude's input-hint footer many lines below and mis-rendered as a menu.
      // Such prose has ordinary text after the list, not a footer, so it fails
      // here. (parsed.lastIdx is the line that broke the option loop.)
      if (parsed && hasNavFooter(lines, parsed.lastIdx, parsed.lastIdx + 2)) {
        return buildResult(lines, anchor, parsed);
      }
    }
    return null;
  }

  // --- arrow-key driver core (pure, browser orchestration lives in app.js) ---

  // Plan the keystrokes to move an arrow-navigable menu's cursor to
  // `targetIndex` (0-based). Strategy: clamp to the top with `optionCount` Up
  // presses (harmless if already at the top — the cursor saturates), then step
  // Down to the target. The caller re-reads the grid and only commits (Enter)
  // once the highlighted row matches, so an over/under-count self-corrects on
  // retry rather than selecting the wrong option. Returns {ups, downs} or null
  // for an out-of-range target.
  function planArrowSteps(optionCount, targetIndex) {
    if (!Number.isInteger(optionCount) || optionCount <= 0) return null;
    if (!Number.isInteger(targetIndex) || targetIndex < 0 || targetIndex >= optionCount) {
      return null;
    }
    return { ups: optionCount, downs: targetIndex };
  }

  // Which option index does the cell-highlight say is currently selected?
  // Returns the single highlighted index, or -1 when none or more than one is
  // marked (ambiguous → the driver must not commit). This is the verify step.
  function highlightedOptionIndex(options) {
    let found = -1;
    for (let i = 0; i < options.length; i++) {
      if (options[i] && options[i].selected) {
        if (found !== -1) return -1;   // ambiguous: >1 highlighted
        found = i;
      }
    }
    return found;
  }

  return {
    detectPrompt,
    planArrowSteps,
    highlightedOptionIndex,
    PROMPT_MAX_OPTIONS,
    PROMPT_SCAN_BACK_LINES,
  };
});
