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
  // The navigation footer claude prints under arrow-navigable menus is the
  // strongest menu-specific signal we have, so it gates the looser detection
  // path (per-option descriptions) against false positives. But a genuine footer is a STANDALONE hint line, not prose that
  // merely mentions one of these phrases — claude discussing a menu in chat
  // ("the footer reads 'Enter to select'") rendered a numbered list above it
  // into a false menu. So require a footer SHAPE: either the arrow-navigation
  // glyphs (which essentially never appear in claude's own prose) or at least
  // two distinct, non-overlapping navigation signals on the one line.
  const NAV_SIGNALS = [
    /↑|↓/,                          // arrow glyphs
    /to navigate/i,
    /esc to (?:cancel|exit)/i,
    /enter to (?:select|confirm)/i,
    /·/,                            // the middot the footer uses to join hints
  ];
  function isNavFooterLine(line) {
    if (/↑|↓/.test(line)) return true;       // arrows alone are decisive
    let n = 0;
    for (const re of NAV_SIGNALS) if (re.test(line)) n++;
    return n >= 2;
  }
  // A column-0 line that is TUI chrome (box borders, dividers, block/selector
  // glyphs, the input composer) rather than the hard-wrapped tail of a prose
  // description. Used to stop a description from swallowing the frame around it.
  const WRAP_STOP = /^\s*(?:[─╌━]{3,}|[│┃┌┐└┘├┤┬┴┼╭╮╰╯▐▛▜▟▝▘█❯⏸])/;

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
      // `\s*` not `\s+`: the real AskUserQuestion card renders its option
      // numbers flush at COLUMN 0 (no leading space), while the first option is
      // matched out-of-loop with the same tolerance. Requiring leading
      // whitespace here dropped every option after the first on a column-0 card
      // → single option → null → the "couldn't parse" fallback. The strict
      // `=== expected` sequence check below is what actually guards against a
      // stray numbered line in prose being mistaken for the next option.
      const m = lines[j].match(/^(\s*)(\d+)\.\s+(.+)$/);
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
      // A column-0 (at-or-left of the number) non-numbered line. Usually the
      // menu is over — but when a long description overflows the card width,
      // xterm HARD-WRAPS its tail onto a fresh row at column 0 with no
      // re-indent, and the old `break` here truncated the option list (often to
      // a single option → null) and left the footer outside the proximity
      // guards, so the whole card silently failed to parse. Fold such a row
      // back into the current description, but only when it (a) directly abuts
      // the description it continues — `last.description` is already non-empty
      // and the previous row was non-blank, so a blank-separated paragraph ends
      // the list — and (b) isn't TUI chrome. Otherwise the list is genuinely
      // over.
      const prevBlank = j === 0 || lines[j - 1].trim() === "";
      if (last.description && !prevBlank && !WRAP_STOP.test(lines[j])) {
        last.description = last.description + " " + lines[j].trim();
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
      if (isNavFooterLine(lines[k])) return true;
    }
    return false;
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

    // No second pass. A glyph-less menu is an AskUserQuestion card, and those
    // are sourced from the structured tool_use record in the transcript
    // (gui/ask_prompt.py) rather than scraped — runDetect() already yields to
    // that banner, and GUI sessions are told not to use the tool at all
    // (core/hooks/startup_prompt_hook.py). The footer-anchored fallback that
    // used to catch them could not distinguish a real card from an ordinary
    // numbered list that happened to sit above a nav-hint line, so dropping it
    // removes a class of false banners rather than any working detection
    // (#T-2026-252).
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
