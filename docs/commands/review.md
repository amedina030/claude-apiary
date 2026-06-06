---
name: review
description: Review changed code for standards conformance, quality, and efficiency, then fix any issues found
user-invocable: true
---

Review recent changes against the project's documentation standards. This command reads the relevant standards docs and uses them as a rubric to evaluate what changed.

## Arguments

- `/review` — review all uncommitted changes (staged + unstaged)
- `/review --staged` — review only staged changes
- `/review HEAD~N` — review the last N commits

## Steps

### 1. Get the changeset

Determine what to review based on the argument:

- No argument: run `git diff` and `git diff --cached` to get all uncommitted changes
- `--staged`: run `git diff --cached` only
- `HEAD~N`: run `git diff HEAD~N`

Parse the diff to get a list of changed files and their changes.

### 2. Run mechanical checks

Run the conformance checker:
```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" docs/check.py
```

Report any issues found.

### 3. Load relevant standards

Resolve the apiary repo path: `apiary_repo = output of python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" --print-repo-path`

Based on what files changed, read the applicable standards:

| Files changed | Standard to load |
|--------------|-----------------|
| Any `*.py` file | `<apiary_repo>/docs/standards/code-style.md` |
| Any file under `docs/` | `<apiary_repo>/docs/standards/doc-style.md` |
| New directory under repo root | `<apiary_repo>/docs/standards/new-tool-checklist.md` |

Read each applicable standard in full using the Read tool.

### 4. Review each changed file

For each changed file, evaluate against the loaded standards. Check:

**For Python files (code-style.md):**
- Naming conventions (files, functions, constants, classes)
- Import ordering and style
- Error handling patterns (hooks: try/except; CLI: argparse)
- Use of `core/` utilities instead of reimplementing
- File structure (docstring, imports, constants, functions, main guard)
- Testing patterns if test files changed
- UTF-8 encoding on all file I/O

**For doc files (doc-style.md):**
- Frontmatter completeness and correctness
- Tone (direct, specific, no fluff)
- Structure (scannable headers, tables for structured data)
- Content (file paths, concrete examples, no speculation)
- Cross-references to other docs where relevant

**For new tools (new-tool-checklist.md):**
- All checklist items addressed
- Directory structure follows convention
- setup.py integration complete
- Docs updated

### 5. Report findings

Output a structured report:

```
## Standards Review

### Mechanical checks
<output from docs/check.py>

### Code style
<findings per file, or "All clear">

### Doc style
<findings per file, or "All clear">

### Checklist compliance
<findings, or "N/A">

### Summary
<count> issues found across <count> files
```

For each issue, include:
- The file and line/section
- Which standard rule it violates
- What to fix

### 6. Fix issues

After presenting the report, fix all issues found. Do not ask for confirmation — the purpose of this command is to review AND fix.
