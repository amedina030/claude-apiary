"""Project key derivation — shared by core hooks and scribe."""
from pathlib import Path


def project_key_from_path(p):
    """Derive Claude's project key from an absolute path.

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
