---
id: list_form_subprocess
title: Use list-form subprocess for long CLI arguments
category: behavioral
requires: []
---
### Use list-form subprocess for long CLI arguments

When invoking any repo CLI tool with a text argument longer than ~3 lines or containing markdown (e.g. `--content`, `--description`, `--problem`, `--note`), **always** use list-form subprocess invocation — never bash with shell quoting.

Bash double-quoted strings trigger command substitution on backticks and break on apostrophes, which collide with virtually any markdown or human-written prose. The list-form pattern bypasses shell interpretation entirely.

**Preferred patterns** (in order):

1. **`subprocess.run` list-form** — pass args as a list, content as an element:
   ```python
   subprocess.run(["python", "scribe/notes.py", "add", "--type", "handoff",
                    "--content", long_text_var], ...)
   ```

2. **Scratch-file approach** — write content to a temp file, pass `--file` or read via stdin:
   ```python
   tmp = Path(tempfile.mktemp(suffix=".md"))
   tmp.write_text(content, encoding="utf-8")
   subprocess.run(["python", "tool.py", "--file", str(tmp)], ...)
   ```

3. **`python -c`** — for one-liners that call a module function directly.

**Never do this** for long/markdown content:
```bash
python scribe/notes.py add --content "text with `backticks` and it's broken"
```

**Why:** This failure mode has recurred across multiple sessions. Shell quoting is inherently fragile for prose content — backticks invoke command substitution, unmatched quotes break the command, and escaping is error-prone. List-form subprocess sidesteps the entire class of bugs.
