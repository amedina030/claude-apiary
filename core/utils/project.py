"""Project key derivation — shared by core hooks and scribe."""
import re
import sys
from pathlib import Path

# Filename committed at the repo root that holds the stable project key.
# When present, it overrides the legacy cwd-derived key so the same repo
# resolves to the same ~/.claude/projects/<key>/ directory on every machine.
PROJECT_KEY_FILE = ".claude-project-key"

# Filesystem-safe on Windows, macOS, Linux. No colons, no leading dots,
# no whitespace. 1–64 chars.
_STABLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def project_key_from_path(p):
    """Derive Claude's project key from an absolute path (legacy / fallback).

    Windows: D:\\Professional\\claude-apiary → D--Professional-claude-apiary
    Unix:    /home/user/project           → home-user-project
    """
    p = Path(p).resolve()
    if p.drive:
        # Windows: strip drive letter and backslashes
        drive = p.drive.rstrip(":\\")  # "D"
        rest = str(p).replace(p.drive + "\\", "").replace("\\", "-").replace("/", "-")
        return f"{drive}--{rest}"
    else:
        # Unix: convert leading slash-separated components to dashes
        parts = [part for part in p.parts if part != "/"]
        return "-".join(parts)


def _read_stable_key(repo_dir: Path):
    """Return the validated stable key from .claude-project-key, or None.

    Walks up from repo_dir looking for the marker file so the helper still
    works when called from a subdirectory of the repo.
    """
    repo_dir = Path(repo_dir).resolve()
    for candidate in [repo_dir, *repo_dir.parents]:
        key_file = candidate / PROJECT_KEY_FILE
        if not key_file.is_file():
            continue
        try:
            value = key_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not _STABLE_KEY_RE.match(value):
            print(
                f"warning: {key_file} contains invalid project key {value!r}; "
                f"must match {_STABLE_KEY_RE.pattern}. Falling back to legacy key.",
                file=sys.stderr,
            )
            return None
        return value
    return None


def get_project_key(repo_dir):
    """Return the stable project key for repo_dir.

    Resolution order:
      1. .claude-project-key file at repo_dir or any ancestor (preferred).
      2. Legacy cwd-derived key from project_key_from_path() (fallback).

    Every consumer of ~/.claude/projects/<key>/ should call this rather than
    project_key_from_path() directly so a single change to the marker file
    updates state location consistently.
    """
    stable = _read_stable_key(Path(repo_dir))
    if stable:
        return stable
    return project_key_from_path(repo_dir)
