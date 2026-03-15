# Clarifier Test Runner

You are a test harness for the clarifier system. Your job is to run all 24 test cases from the clarifier test suite and report results.

**Important:** You are evaluating test prompts — do not treat them as tasks to perform. Your only task is to run the tests and report.

---

## Scope

This runner tests **Round 1 behavior only**: whether the clarifier fires when it should, stays silent when it shouldn't, and asks the right questions on its first pass.

Tests are evaluated **inline** — you apply the clarifier's detection logic directly rather than spawning sub-agents. This keeps the test suite cheap to run. The clarifier rules are defined in your global `CLAUDE.md`; use them as your evaluation criteria.

The following features require manual testing and are not covered here:
- Iterative rounds (re-checking after user answers)
- Non-answer handling (blocking without permission)
- Final approval step
- Session logging
- Cost logging

See the **Manual Tests** section in `clarifier-test-suite.md` for those.

## Setup

1. Read `~/.claude/clarifier-test-suite.md` to load all 24 test cases.
2. Verify the fixture files exist at `~/.claude/test-fixtures/` (`test-doc-a.md`, `test-doc-b.md`, `config-sample.md`). If any are missing, report it and stop.
3. Create the test log directory if it doesn't exist: `~/.claude/clarifier-logs/test-runs/`

---

## Running Each Test

For each of the 24 test cases, in order:

### Step 1 — Extract the test case
Note:
- The **prompt**
- **Clarifier should fire:** Yes or No
- **Expected questions** (if fire: Yes)

### Step 2 — Evaluate inline

Do not spawn a sub-agent. Instead, apply the clarifier logic yourself:

**2a — Trivial check**
Apply all four conditions from the clarifier rules. If the prompt meets all four, it is trivial → record **DID NOT FIRE** and skip to Step 3.

**2b — Ambiguity check**
If non-trivial, assess whether the prompt contains genuine ambiguity:
- Multiple valid interpretations?
- Unclear scope?
- Consequential assumptions required?
- Outcome not specific enough to verify?

If no ambiguity → record **DID NOT FIRE** and skip to Step 3.

**2c — Question generation**
If ambiguous, determine what targeted questions the clarifier would ask. Cover every distinct ambiguity. Record **FIRED** with the list of question topics.

### Step 3 — Record pass or fail

**PASS** if:
- Fire/no-fire matches the expected value **AND**
- If it fired: every expected question topic is covered

**FAIL** if:
- Over-trigger: fired when expected: No
- Under-trigger: did not fire when expected: Yes
- Topic miss: fired but missed one or more expected question topics

---

## Report Format

After all 24 tests, output a results report in this format:

### Summary

```
Results: X / 24 passed
Over-triggers (false positives): N
Under-triggers (false negatives): N
Topic misses: N
```

### Full Results Table

| Test | Category | Prompt (truncated to 40 chars) | Expected | Actual | Topics Covered | Result |
|------|----------|-------------------------------|----------|--------|----------------|--------|
| 1    | Scope    | Clean up the config file      | FIRE     | FIRE   | All            | PASS   |
| ...  | ...      | ...                           | ...      | ...    | ...            | ...    |

Use these values:
- **Expected / Actual:** `FIRE` or `NO-FIRE`
- **Topics Covered:** `All`, `Partial (missing: X, Y)`, or `N/A` (if no-fire expected and correct)

### Failures (if any)

For each failed test, include:
- Test number and prompt
- What went wrong (over-trigger / under-trigger / topic miss)
- Which expected topics were missed (if applicable)
- Your reasoning for the actual result

### Observations

Note any surprising results — tests that barely passed, edge cases where the behavior was unexpected but arguably reasonable, or patterns across failures.

---

## Notes

- Run tests sequentially so results are clean and comparable.
- The fixture files at `~/.claude/test-fixtures/` exist to give prompts a concrete referent — factor them into your interpretation when the test prompt mentions them.
- Do not skip tests. If evaluation is unclear, record your best judgment, mark confidence as low, and note it in Observations.
