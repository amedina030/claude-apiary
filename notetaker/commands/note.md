Append a note to `.claude/notes.md` in the current working directory.

## Steps

1. Run this bash command to get the current timestamp:
   ```bash
   date -u +"%Y-%m-%d %H:%M UTC"
   ```

2. Read `.claude/notes.md` if it exists to determine the next note number.
   - Count the number of existing numbered entries (lines starting with a digit and a dot)
   - The next number is that count + 1
   - If the file does not exist, start at 1

3. Append the following line to `.claude/notes.md` (create the file if it does not exist):
   ```
   {n}. [{timestamp}] [session: {short_session_id}] {arguments}
   ```
   Where:
   - `{n}` is the next note number
   - `{timestamp}` is the output from step 1
   - `{short_session_id}` is the first 8 characters of the current session ID
   - `{arguments}` is the full text passed to this command (`$ARGUMENTS`)

4. Do not respond to the user. Return immediately to whatever you were doing before.
