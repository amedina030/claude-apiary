// Unit tests for prompt_detector.js. Run with `node test_prompt_detector.js`
// (no npm dependencies). The detector operates on the rendered-and-trimmed
// xterm buffer as a string[]; these fixtures model the shapes we've observed.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { detectPrompt } = require("./prompt_detector");

// --- helpers --------------------------------------------------------------

function linesOf(text) {
  // Literal block → string[] with trailing-whitespace trim (matches what
  // xterm's translateToString(true).replace(/\s+$/, "") produces).
  return text.split("\n").map((s) => s.replace(/\s+$/, ""));
}

// --- fixtures -------------------------------------------------------------

// Trust folder (2 options). Claude Code places the question directly above
// the options, with no interstitial context line — matches the capture that
// T-2026-153 notes "parses correctly".
const TRUST_FOLDER = linesOf(`
Do you trust the files in this folder?

❯ 1. Yes, proceed
  2. No, exit

`);

// Tool-permission Bash (3 options). NOTE: The real prompt may include the
// command on an intermediate line between question and options; the detector
// currently picks "last non-empty line above options" as the question, so a
// real capture is needed to settle the shape. Tracked as a follow-up.
// This fixture models the simple shape (question directly above options).
const TOOL_PERMISSION_BASH = linesOf(`
Allow Bash command?

❯ 1. Yes
  2. Yes, and don't ask again for this command
  3. No, and tell Claude what to do differently

`);

// Plan-mode approval (4 options). The plan body is framed by ╌-dividers; the
// detector should extract it into the context field.
const PLAN_MODE_APPROVAL = linesOf(`
────────────────────────────────────────
⏸ plan mode on

╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  ## Goal
  Rewrite the auth middleware to stop
  storing session tokens in plaintext.

  ## Steps
  1. Move tokens to encrypted store
  2. Update login handler
  3. Add migration
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

Would you like to proceed?

❯ 1. Yes, proceed
  2. Yes, and auto-accept edits
  3. No, keep planning
  4. Tell Claude what to change with this feedback

`);

// Same shape as TOOL_PERMISSION_BASH but with the literal '>' selector
// claude-code 2.1.116+ emits instead of ❯. This is the .claude/ protect-self
// gate prompt observed during the L-2026-111 investigation.
const PROTECT_SELF_GATE = linesOf(`
Do you want to create test.txt?

>  1. Yes
   2. Yes, and allow Claude to edit its own settings for this session
   3. No

`);

// Single ❯ with no second option → should NOT match (needs >=2 options).
const NOT_A_PROMPT_SINGLE = linesOf(`
Some status text

❯ 1. lonely option

`);

// Options out of order (1, 3, 4) → only option 1 matches; bails out.
const OUT_OF_ORDER = linesOf(`
What now?

❯ 1. First
  3. Third
  4. Fourth

`);

// No ❯ marker at all — buffer with arbitrary chatter.
const NO_MARKER = linesOf(`
random
text without any numbered prompt
hello
`);

// --- tests ----------------------------------------------------------------

test("trust-folder: 2 options, no plan body", () => {
  const r = detectPrompt(TRUST_FOLDER);
  assert.ok(r, "should detect a prompt");
  assert.equal(r.question, "Do you trust the files in this folder?");
  assert.equal(r.options.length, 2);
  assert.deepEqual(
    r.options.map((o) => [o.number, o.text, o.selected]),
    [
      [1, "Yes, proceed", true],
      [2, "No, exit", false],
    ]
  );
  assert.equal(r.context, "", "no ╌-framed body → empty context");
});

test("tool-permission: 3 options, question present, no context", () => {
  const r = detectPrompt(TOOL_PERMISSION_BASH);
  assert.ok(r);
  assert.equal(r.question, "Allow Bash command?");
  assert.equal(r.options.length, 3);
  assert.equal(r.options[0].selected, true);
  assert.equal(r.options[2].text, "No, and tell Claude what to do differently");
  assert.equal(r.context, "");
});

test("plan-mode: 4 options plus extracted plan body in context", () => {
  const r = detectPrompt(PLAN_MODE_APPROVAL);
  assert.ok(r);
  assert.equal(r.question, "Would you like to proceed?");
  assert.equal(r.options.length, 4);
  assert.match(r.context, /Rewrite the auth middleware/);
  assert.match(r.context, /## Steps/);
  assert.ok(
    !r.context.includes("⏸ plan mode on"),
    "chrome above the top ╌-divider should not leak into context"
  );
});

test("protect-self gate: '>' selector (2.1.116+) parses same as ❯", () => {
  const r = detectPrompt(PROTECT_SELF_GATE);
  assert.ok(r, "should detect a prompt with '>' selector");
  assert.equal(r.question, "Do you want to create test.txt?");
  assert.equal(r.options.length, 3);
  assert.equal(r.options[0].selected, true);
  assert.equal(r.options[0].text, "Yes");
  assert.equal(
    r.options[1].text,
    "Yes, and allow Claude to edit its own settings for this session"
  );
  assert.equal(r.options[2].text, "No");
});

test("single-option block is not treated as a prompt", () => {
  const r = detectPrompt(NOT_A_PROMPT_SINGLE);
  assert.equal(r, null);
});

test("out-of-order numbering stops at the first gap", () => {
  const r = detectPrompt(OUT_OF_ORDER);
  // Only option 1 was parsed; 3+4 violate the sequence → under the 2-option
  // minimum → returns null.
  assert.equal(r, null);
});

test("buffer without a ❯ marker returns null", () => {
  const r = detectPrompt(NO_MARKER);
  assert.equal(r, null);
});

test("signature is stable across calls on the same fixture", () => {
  const a = detectPrompt(PLAN_MODE_APPROVAL);
  const b = detectPrompt(PLAN_MODE_APPROVAL);
  assert.equal(a.signature, b.signature);
});

test("signature changes when options differ", () => {
  const a = detectPrompt(TRUST_FOLDER);
  const b = detectPrompt(TOOL_PERMISSION_BASH);
  assert.notEqual(a.signature, b.signature);
});
