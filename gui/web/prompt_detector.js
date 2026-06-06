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

  function detectPrompt(lines) {
    for (let i = lines.length - 1; i >= 0; i--) {
      // Selector char: claude-code historically rendered ❯ (U+276F); 2.1.116+
      // emits a literal '>' instead. Accept either so the detector keeps
      // working across versions. Capture the prefix (group 1) so we know the
      // column the option number sits at — description lines indent past it.
      const sel = lines[i].match(/^(\s*[❯>]\s*)(\d+)\.\s+(.+)$/);
      if (!sel) continue;
      const firstNum = parseInt(sel[2], 10);
      const options = [{
        number: firstNum,
        text: sel[3].trim(),
        description: "",
        selected: true,
        indent: sel[1].length,
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
            selected: false,
            indent: m[1].length,
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
      if (options.length < 2) continue;
      // Guard the looser description path against false positives: a menu that
      // carries per-option descriptions must also show a navigation footer
      // (claude prints "Enter to select · ↑/↓ to navigate · Esc to cancel").
      // Classic description-less numbered prompts keep the old, footer-free
      // acceptance so plan-mode / trust-folder / permission shapes don't regress.
      if (sawDescription) {
        const navHint = /↑|↓|to select|to navigate|esc to cancel|enter to (?:select|confirm)/i;
        let hasNav = false;
        for (let k = i; k < Math.min(lines.length, lastIdx + 3); k++) {
          if (navHint.test(lines[k])) { hasNav = true; break; }
        }
        if (!hasNav) continue;
      }
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
      const signature = (context ? context.slice(0, 80) + "|" : "") +
                        options.map(o => `${o.number}.${o.text}`).join("|");
      return { question, context, options, signature };
    }
    return null;
  }

  return { detectPrompt, PROMPT_MAX_OPTIONS, PROMPT_SCAN_BACK_LINES };
});
