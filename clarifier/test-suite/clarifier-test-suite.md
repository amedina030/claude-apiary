# Clarifier Test Suite

## Overview

This file contains 24 test cases for validating clarifier behavior. Each test specifies a prompt to send to an executing agent, whether the clarifier should fire, and what questions it should ask if it does.

Test fixture files are located at `~/.claude/test-fixtures/`:
- `test-doc-a.md` — design notes doc with duplicates and resolved items
- `test-doc-b.md` — release checklist
- `config-sample.md` — configuration reference with incomplete sections

**Note:** The automated test runner uses inline evaluation — no clarifier sub-agents are spawned. Fixture file references in test prompts are factored into the inline interpretation. See `testing.md` for details.

## What These Tests Cover

The 24 automated test cases validate **Round 1 behavior only** — whether the clarifier fires when it should, stays silent when it shouldn't, and asks the right questions on its first pass.

The following features of the full clarifier flow are **not covered** by automated tests and must be verified manually:
- Iterative rounds (does it correctly re-check after you answer?)
- Non-answer handling (does it block and ask permission before proceeding?)
- Final approval step (does it present the refined prompt and require sign-off?)
- Session logging (does it write a valid log to `clarifier-logs/` with correct filename format and UUIDv7?)
- Cost logging (does the executing agent append a cost entry to `cost.log` with correct fields?)

See the **Manual Tests** section at the bottom of this file for guidance on testing those features.

## Checking Results

**Manual:** Read the clarifier output and compare against expected questions below.
**Automated:** Feed each prompt to an executing agent via `/run-clarifier-tests`; the runner checks that (a) it fired when expected, and (b) its Round 1 questions cover the expected topics.

## Test Logs

The automated test runner does not produce session logs (no agents are spawned). The test log directory `~/.claude/clarifier-logs/test-runs/` is created during setup for use by any future agent-based tests.

Real clarifier session logs from actual usage go to `~/.claude/clarifier-logs/`.

---

## Category 1: Should Fire — Scope Ambiguity

### Test 1 — Scope: Clean up file

**Prompt:** `Clean up the config file.`

**Clarifier should fire:** Yes

**Expected questions:**
- Which config file? (no file is specified)
- What does "clean up" mean — formatting only, or content changes too?
- Should incomplete sections (e.g., Cache) be filled in or left alone?

---

### Test 2 — Scope: Update documentation

**Prompt:** `Update the documentation.`

**Clarifier should fire:** Yes

**Expected questions:**
- Which documentation file?
- What kind of updates — new content, corrections, formatting?
- Is there a specific section to target?

---

### Test 3 — Scope: Refactor

**Prompt:** `Refactor the code in test-doc-a.md.`

**Clarifier should fire:** Yes

**Expected questions:**
- test-doc-a.md is a markdown doc, not code — clarify what "refactor" means here
- Which section or content should be restructured?
- What is the desired end state?

---

### Test 4 — Scope: Improve a document

**Prompt:** `Improve test-doc-a.md.`

**Clarifier should fire:** Yes

**Expected questions:**
- What does "improve" mean — structure, content, clarity, completeness?
- Which sections should be targeted?
- Are there specific problems to fix, or is this open-ended?

---

## Category 2: Should Fire — Pronoun / Referent Ambiguity

### Test 5 — Referent: Move it

**Prompt:** `Move it to the other section.`

**Clarifier should fire:** Yes

**Expected questions:**
- What is "it"? (no prior context establishes a referent)
- Which file is this in?
- Which section is "the other section"?

---

### Test 6 — Referent: Delete the duplicate

**Prompt:** `Delete the duplicate in test-doc-a.md.`

**Clarifier should fire:** Yes

**Expected questions:**
- Which duplicate — Section 1 has a duplicate Decision A, Section 2 has a duplicate Q3
- Should both copies be removed, or just one — and if one, which?

---

### Test 7 — Referent: Swap them

**Prompt:** `Swap them.`

**Clarifier should fire:** Yes

**Expected questions:**
- Swap what? No referent established
- In which file?
- What does "swap" mean here — reorder, exchange content, rename?

---

### Test 8 — Referent: Put it back

**Prompt:** `Put it back.`

**Clarifier should fire:** Yes

**Expected questions:**
- What is "it"?
- Back where? No prior state or location established
- Which file is involved?

---

## Category 3: Should Fire — Conflicting Constraints

### Test 9 — Conflict: Shorter but more comprehensive

**Prompt:** `Make test-doc-a.md shorter but more comprehensive.`

**Clarifier should fire:** Yes

**Expected questions:**
- These goals conflict — which takes priority if they can't both be achieved?
- What should be cut to make it shorter?
- What should be added to make it more comprehensive?

---

### Test 10 — Conflict: Simplify but keep everything

**Prompt:** `Simplify config-sample.md but don't remove any options.`

**Clarifier should fire:** Yes

**Expected questions:**
- If nothing can be removed, what does "simplify" mean here — formatting, wording, structure?
- Are new options allowed to be added, or is the field set frozen?

---

### Test 11 — Conflict: Faster without structural change

**Prompt:** `Make it faster without changing the structure.`

**Clarifier should fire:** Yes

**Expected questions:**
- What is "it"? No referent established
- What does "faster" mean in the context of a markdown document?
- What counts as a structural change?

---

### Test 12 — Conflict: Rewrite but keep the same

**Prompt:** `Rewrite test-doc-b.md to be cleaner but keep everything exactly the same.`

**Clarifier should fire:** Yes

**Expected questions:**
- "Rewrite" and "keep everything exactly the same" are contradictory — clarify which takes priority
- What specifically should change to make it "cleaner"?

---

## Category 4: Should Fire — Implicit Assumptions

### Test 13 — Implicit: Delete resolved items

**Prompt:** `Delete the resolved items in test-doc-a.md.`

**Clarifier should fire:** Yes

**Expected questions:**
- Which items count as "resolved"? (some are marked RESOLVED, but the criteria aren't defined)
- Should resolved items be deleted entirely, archived, or moved to a separate section?
- Decision A appears twice as RESOLVED — delete both occurrences?

---

### Test 14 — Implicit: Move important sections to top

**Prompt:** `Move the important sections to the top of test-doc-a.md.`

**Clarifier should fire:** Yes

**Expected questions:**
- Which sections are "important"? This is a judgment call
- Top of the file, or top of a specific section?
- Should unimportant sections be removed or just reordered?

---

### Test 15 — Implicit: Add missing fields

**Prompt:** `Add the missing fields to config-sample.md.`

**Clarifier should fire:** Yes

**Expected questions:**
- Which fields are "missing"? Missing relative to what standard or schema?
- The Cache section says "fields to be determined" — does that count as missing?
- Should new sections be added, or only fields within existing sections?

---

### Test 16 — Implicit: Fix inconsistencies

**Prompt:** `Fix the inconsistencies in test-doc-a.md.`

**Clarifier should fire:** Yes

**Expected questions:**
- Which inconsistencies? (duplicates? formatting? conflicting decisions?)
- What is the source of truth to fix against?
- Should duplicate entries be merged or removed?

---

## Category 5: Should NOT Fire — Clearly Scoped

### Test 17 — Clear: Read a file

**Prompt:** `Read test-doc-a.md.`

**Clarifier should fire:** No

**Why it's clear:** Single named file, single action (read), zero assumptions needed, fully reversible.

---

### Test 18 — Clear: Append a specific line

**Prompt:** `Add a line saying "Last updated: 2026-03-13" to the bottom of test-doc-b.md.`

**Clarifier should fire:** No

**Why it's clear:** Exact content specified, exact location specified, single named file, easily undone.

---

### Test 19 — Clear: Count sections

**Prompt:** `How many sections are in test-doc-a.md?`

**Clarifier should fire:** No

**Why it's clear:** Read-only question, single named file, one unambiguous answer.

---

### Test 20 — Clear: Show file contents

**Prompt:** `What is in config-sample.md?`

**Clarifier should fire:** No

**Why it's clear:** Read-only, single named file, no judgment required.

---

## Category 6: Should NOT Fire — Trivially Obvious

### Test 21 — Trivial: List files

**Prompt:** `What files are in this directory?`

**Clarifier should fire:** No

**Why it's clear:** Unambiguous read-only system query, no assumptions, no file modifications.

---

### Test 22 — Trivial: Show first lines

**Prompt:** `Show me the first 5 lines of test-doc-a.md.`

**Clarifier should fire:** No

**Why it's clear:** Exact line count specified, single named file, read-only.

---

### Test 23 — Trivial: Status check

**Prompt:** `Is the clarifier on?`

**Clarifier should fire:** No

**Why it's clear:** Yes/no system state query, zero assumptions, no file modifications.

---

### Test 24 — Trivial: Off-topic general question

**Prompt:** `How do I make a cake?`

**Clarifier should fire:** No

**Why it's clear:** General knowledge question, no files involved, no assumptions needed, no actions taken.

---

## Pass / Fail Criteria

A test **passes** if:
- The clarifier fired when expected (or stayed silent when expected)
- If it fired, its Round 1 questions cover all the topics listed under **Expected questions** (wording need not match exactly — topic coverage is what matters)

A test **fails** if:
- The clarifier fired when it shouldn't have (over-trigger)
- The clarifier stayed silent when it should have fired (under-trigger / false negative)
- The clarifier fired but missed a key expected question topic

---

## Manual Tests — Interactive Flow Features

These tests cannot be automated. Run them manually by sending the prompt as a real request in a Claude Code session with the clarifier ON.

### MT-1 — Iterative re-check

**Setup:** Enable clarifier. Send an ambiguous prompt. Answer the first round of questions with answers that are still slightly vague.

**Expected behavior:** The clarifier re-checks the updated prompt, detects remaining ambiguity, and asks a second round of questions rather than proceeding.

**Pass:** A second round of questions appears.
**Fail:** The clarifier proceeds after one round despite remaining ambiguity.

---

### MT-2 — Non-answer handling

**Setup:** Enable clarifier. Send an ambiguous prompt. When the clarifier asks questions, respond: "I don't know, just do whatever you think is best."

**Expected behavior:** The clarifier asks explicitly: "Would you like me to proceed with this ambiguity unresolved, or would you like to provide more direction?" It does not proceed until you answer that question.

**Pass:** Clarifier blocks and asks permission before proceeding.
**Fail:** Clarifier proceeds without explicit permission.

---

### MT-3 — Final approval step

**Setup:** Enable clarifier. Send an ambiguous prompt and answer all questions fully.

**Expected behavior:** Once ambiguity is resolved, the clarifier presents the final cleaned-up prompt and asks "Do you approve this?" Claude does not act until you confirm.

**Pass:** Final prompt is shown and approval is explicitly requested before Claude acts.
**Fail:** Claude acts before you see or approve the final prompt.

---

### MT-4 — Session logging

**Setup:** Enable clarifier. Complete a full clarification session through to approval.

**Expected behavior:** A log file is created at `~/.claude/clarifier-logs/` (or `.claude/clarifier-logs/` in the project if that directory exists) with the filename format `clarifier-YYYY-MM-DD-HHMMSS-XXXX.md` (timestamp + 4 random hex chars). The log contains a UUIDv7 in the header, the original prompt, all clarification rounds, the final approved prompt, and outcome.

**Pass:** Log file exists with correct filename format, contains a UUIDv7 field, and all required sections.
**Fail:** No log file created, filename format is wrong, UUIDv7 is missing, or log is missing key sections.

---

### MT-5 — Iteration limit

**Setup:** Enable clarifier. Send an ambiguous prompt. Give partial or evasive answers for 5 consecutive rounds.

**Expected behavior:** After round 5, the clarifier pauses and shows the current state of the prompt plus remaining ambiguities, then asks whether to continue clarifying or proceed as-is.

**Pass:** The limit message appears after exactly 5 rounds.
**Fail:** Clarifier continues indefinitely or stops early.

---

### MT-6 — Cost logging

**Setup:** Enable clarifier. Complete a full clarification session through to approval.

**Expected behavior:** After the clarifier hands off, the executing agent appends a line to `~/.claude/clarifier-logs/cost.log` containing: timestamp, UUIDv7, total_tokens, tool_uses, duration_ms, log filename, and the first 80 chars of the original prompt. The UUIDv7 and log filename should match those in the session log from MT-4.

**Pass:** `cost.log` entry exists with all required fields; UUIDv7 matches the session log.
**Fail:** No entry written, fields are missing, or UUIDv7 does not match the session log.
