---
name: prose
description: Check a markdown file or note text for machine-writing tells with the prose linter, then rewrite until it reads like a person wrote it
user-invocable: true
---

# /prose — check and rewrite prose

Runs the prose linter (`prose/cli.py`) on a file and rewrites it until the report is clean at `warning` or above. The linter is markup-aware: paragraphs and list items are scored by different rules, code and tables are skipped. The rules and their reasons are in `docs/standards/prose-style.md`.

## Usage

```
/prose <path>          # check and rewrite one markdown file
/prose <path> --check  # report only, no rewrite
```

## Steps

1. Run the check via the launcher and read the report:

   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" prose/cli.py check <path>
   ```

2. For each finding, apply the fix the message names (cut the announcement, split the sentence, keep the positive half, use the plain word). Do not paraphrase around a finding by swapping words while keeping the shape: every rule is a rhythm, and the shape is what it counts.

3. Re-run the check. Repeat until the file reports `clean` or only `suggestion`-level findings remain that you can justify in one line.

4. Then read the file once for the three tells no rule can count, and fix them by hand:
   - an aphoristic closer (a sentence that would work as a pull quote): delete it
   - a mirror construction (a word repeated in a balanced frame to sound considered): say it once
   - a reflexive triad (three adjectives or clauses where one carries the meaning): keep the one

5. Report what changed as a short list of before/after pairs, and the final `check` output line.

With `--check`, stop after step 1 and report the findings.

## Notes

- `prose/cli.py rules --verbose` prints every rule with the reason its threshold has the value it has.
- `prose/cli.py calibrate <dir>` measures a rule change against a corpus; an `error`-level rule that fires on accepted prose is miscalibrated.
- The same check runs automatically on every markdown Write/Edit (advisory) and on every `scribe/notes.py add`/`learn`. The per-repo flag `prose-gate` (`core/flags.py enable prose-gate`) turns error-level findings into a block.
