# Budgeter Setup

Sets up claude-apis budgeter hooks for the current project.

## Steps

1. Locate the claude-apis repo:
   - Search for `setup.py` containing the text `claude-apis` by looking in common locations: any `claude-apis` directory under the home directory, or any parent/sibling of the current working directory.
   - If found, use that path as `<apis-root>`.
   - If not found, ask the user: "Where is the claude-apis repo located? Please provide the absolute path."

2. Use the current working directory as the project path to configure.

3. Run setup:
   ```
   python <apis-root>/setup.py --project-path "<current-working-directory>"
   ```

4. Report what was configured: the settings.json path, the Python executable used, and the claude-apis location.

5. Remind the user to start a new Claude Code session for the hooks to activate.
