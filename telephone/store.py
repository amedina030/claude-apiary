"""Central record store for telephone calls.

Every call gets one markdown file under
``<main-apiary>/.apiary/telephone/<year>/<seq>.md`` with the id
``C-<year>-<seq>``. The store is central rather than per-repo because a call
has two sides and neither owns it: the caller's session started it, the
callee's checkout answered it, and the user wants one place to read both back.
The GUI's ``<main-apiary>/.apiary/gui/`` is the same idea (``gui/paths.py``).

Layout::

    .apiary/telephone/
      <year>/
        next_seq       per-year counter, consumed under a FileLock
        <seq>.md       one call, frontmatter plus one section per exchange

The file shape follows ``researcher/store.py``: a frontmatter block written and
read through ``core.frontmatter``, then a body that is never reinterpreted.
The per-year counter follows ``scribe/store.py::_increment_seq``.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import frontmatter  # noqa: E402
from core.utils.atomic import write_text_atomic  # noqa: E402
from core.utils.filelock import FileLock  # noqa: E402
from core.utils.state import resolve_apiary_repo  # noqa: E402
from core.utils.timeutil import now_iso  # noqa: E402,F401 — re-exported for callers

#: Where the store hangs off main-apiary. ``.apiary/`` (not ``.repos/``) so
#: ``core/doctor.py::check_orphans``, which walks ``.repos/`` looking for
#: folders with no registry entry, never sees these.
STORE_SUBPATH = (".apiary", "telephone")

NEXT_SEQ_FILENAME = "next_seq"
ID_PREFIX = "C"
CONFIG_FILENAME = "config.json"

#: Statuses a record can carry. ``open`` is written before the callee starts.
STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"
STATUS_TIMED_OUT = "timed_out"
STATUS_FAILED = "failed"
STATUS_HUNG_UP = "hung_up"
CLOSED_STATUSES = (STATUS_ANSWERED, STATUS_TIMED_OUT, STATUS_FAILED, STATUS_HUNG_UP)

MODE_ANSWER = "answer"
MODE_ACT = "act"

_ID_RE = re.compile(r"^C-(\d{4})-(\d+)$", re.IGNORECASE)
_EXCHANGE_RE = re.compile(r"^## Exchange (\d+)\b", re.MULTILINE)

#: Defaults used when ``config.json`` is missing or malformed. Mirrors the
#: shipped file so a broken read degrades to working behaviour rather than a
#: traceback (the same fallback shape ``runner/config_loader.py`` uses).
DEFAULT_CONFIG: dict[str, Any] = {
    "max_autonomous_calls_per_session": 3,
    "max_autonomous_exchanges_per_line": 6,
    "model": "",
    "answer": {
        "timeout_seconds": 600,
        "max_turns": 30,
        "permission_mode": "",
        "allowed_tools": ["Read", "Glob", "Grep"],
        "disallowed_tools": ["Write", "Edit", "NotebookEdit", "Bash(git push *)"],
    },
    "act": {
        "timeout_seconds": 1800,
        "max_turns": 150,
        "permission_mode": "acceptEdits",
        "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git *)"],
        "disallowed_tools": ["Bash(git push *)", "Bash(gh pr create *)"],
    },
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def config_path() -> Path:
    return Path(__file__).resolve().parent / CONFIG_FILENAME


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Read ``telephone/config.json``, falling back to :data:`DEFAULT_CONFIG`.

    Per-mode blocks are merged over the defaults key by key, so a config that
    sets only ``answer.timeout_seconds`` still gets the shipped tool lists.
    """
    source = Path(path) if path is not None else config_path()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    merged: dict[str, Any] = dict(DEFAULT_CONFIG)
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def mode_config(config: dict[str, Any], mode: str) -> dict[str, Any]:
    """The per-mode block, with the shipped defaults underneath it."""
    base = DEFAULT_CONFIG.get(mode)
    block = config.get(mode)
    if not isinstance(base, dict):
        raise ValueError(f"unknown telephone mode {mode!r}")
    return {**base, **block} if isinstance(block, dict) else dict(base)


# --------------------------------------------------------------------------- #
# Paths and ids
# --------------------------------------------------------------------------- #


def store_dir(apiary_repo: Path | None = None) -> Path:
    """``<main-apiary>/.apiary/telephone/``. Never created as a side effect."""
    apiary = Path(apiary_repo) if apiary_repo is not None else resolve_apiary_repo()
    return apiary.joinpath(*STORE_SUBPATH)


def year_dir(year: int, apiary_repo: Path | None = None) -> Path:
    return store_dir(apiary_repo) / str(year)


def ensure_layout(year: int, apiary_repo: Path | None = None) -> Path:
    """Create the store root, the year folder and its counter. Returns the year dir."""
    folder = year_dir(year, apiary_repo)
    folder.mkdir(parents=True, exist_ok=True)
    seq_path = folder / NEXT_SEQ_FILENAME
    if not seq_path.exists():
        seq_path.write_text(str(_scan_max_seq(folder) + 1), encoding="utf-8")
    return folder


def _scan_max_seq(folder: Path) -> int:
    """Highest ``<seq>.md`` already in *folder*, or 0.

    Rebuilding from the files rather than from zero means a deleted counter
    cannot hand out an id that would overwrite an existing record.
    """
    best = 0
    try:
        entries = list(folder.glob("*.md"))
    except OSError:
        return 0
    for path in entries:
        if path.stem.isdigit():
            best = max(best, int(path.stem))
    return best


def _increment_seq(folder: Path) -> int:
    """Consume and return the next sequence number for *folder*.

    Locked on the counter file, exactly as ``scribe/store.py::_increment_seq``
    does, so two calls placed at the same moment get distinct ids.
    """
    seq_path = folder / NEXT_SEQ_FILENAME
    with FileLock(seq_path):
        try:
            current = int(seq_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            current = _scan_max_seq(folder) + 1
        seq_path.write_text(str(current + 1), encoding="utf-8")
    return current


def format_id(year: int, seq: int) -> str:
    return f"{ID_PREFIX}-{year}-{seq}"


def parse_id(call_id: str) -> tuple[int, int]:
    """``C-2026-7`` -> ``(2026, 7)``. Raises ``ValueError`` on anything else."""
    match = _ID_RE.match((call_id or "").strip())
    if match is None:
        raise ValueError(f"not a call id: {call_id!r} (expected C-<year>-<seq>)")
    return int(match.group(1)), int(match.group(2))


def record_path(call_id: str, apiary_repo: Path | None = None) -> Path:
    year, seq = parse_id(call_id)
    return year_dir(year, apiary_repo) / f"{seq}.md"


def allocate_id(apiary_repo: Path | None = None, now: datetime | None = None) -> str:
    """Create the layout if needed and consume the next id for this year."""
    stamp = now or datetime.now(timezone.utc)
    folder = ensure_layout(stamp.year, apiary_repo)
    return format_id(stamp.year, _increment_seq(folder))


# --------------------------------------------------------------------------- #
# Record I/O
# --------------------------------------------------------------------------- #


def write_record(path: Path, meta: dict[str, Any], body: str) -> None:
    """Write one record file, creating parent directories as needed.

    Every value is stringified: the frontmatter dialect loads scalars back as
    strings, so writing an int would break the round trip callers rely on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flat = {k: ("" if v is None else str(v)) for k, v in meta.items()}
    content = frontmatter.dump(flat, body)
    if not content.endswith("\n"):
        content += "\n"
    # Atomic: a reader polling `status` while a long act call is running must
    # never catch the record half-written.
    write_text_atomic(path, content)


def read_record(path: Path) -> tuple[dict[str, Any], str]:
    """Split a record file into ``(frontmatter, body)``.

    Strict: a record apiary wrote always has fences, so a file without them is
    a corrupted record rather than a legacy shape to tolerate.
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        return frontmatter.parse(text, strict=True)
    except frontmatter.FrontmatterError as exc:
        raise ValueError(f"{path} has unreadable frontmatter: {exc}") from exc


def load(call_id: str, apiary_repo: Path | None = None) -> tuple[Path, dict[str, Any], str]:
    """Return ``(path, frontmatter, body)`` for *call_id*.

    Raises ``FileNotFoundError`` when the record does not exist and
    ``ValueError`` when the id is malformed.
    """
    path = record_path(call_id, apiary_repo)
    if not path.is_file():
        raise FileNotFoundError(f"no telephone record {call_id} at {path}")
    meta, body = read_record(path)
    return path, meta, body


def list_records(
    apiary_repo: Path | None = None,
    *,
    status: str | None = None,
    callee: str | None = None,
    mode: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every record's frontmatter, newest id first, optionally filtered.

    Frontmatter only: listing must never pay for the exchange bodies, which is
    what keeps ``telephone/cli.py list`` cheap in a caller's context.
    """
    root = store_dir(apiary_repo)
    rows: list[tuple[int, int, dict[str, Any]]] = []
    if not root.is_dir():
        return []
    for folder in sorted(root.iterdir()):
        if not (folder.is_dir() and folder.name.isdigit()):
            continue
        for path in folder.glob("*.md"):
            if not path.stem.isdigit():
                continue
            try:
                meta, _body = read_record(path)
            except (OSError, ValueError):
                continue
            meta = {**meta, "path": str(path)}
            if status and meta.get("status") != status:
                continue
            if callee and meta.get("callee") != callee:
                continue
            if mode and meta.get("mode") != mode:
                continue
            rows.append((int(folder.name), int(path.stem), meta))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    out = [meta for _y, _s, meta in rows]
    return out[:limit] if limit else out


def open_act_call(callee: str, apiary_repo: Path | None = None) -> str | None:
    """The id of an act call still open against *callee*, or ``None``.

    One act call per callee at a time: two unattended sessions editing the same
    checkout would interleave their commits on one branch.
    """
    for meta in list_records(apiary_repo, status=STATUS_OPEN, callee=callee, mode=MODE_ACT):
        call_id = str(meta.get("id") or "")
        if call_id:
            return call_id
    return None


# --------------------------------------------------------------------------- #
# Exchanges
# --------------------------------------------------------------------------- #


def exchange_count(body: str) -> int:
    """How many ``## Exchange N`` sections *body* holds."""
    return len(_EXCHANGE_RE.findall(body or ""))


def render_exchange(
    number: int,
    *,
    sent: str,
    reply: str,
    status: str,
    at: str,
    duration_s: float | int | None = None,
    notes: list[str] | None = None,
) -> str:
    """One exchange section, in the shape :func:`parse_exchanges` reads back.

    The two texts sit inside ``<message>`` / ``<reply>`` tags rather than under
    headings, because a callee reply is itself three markdown headings and a
    heading-delimited reader would stop at the first of them.
    """
    lines = [f"## Exchange {number}", "", f"- at: {at}", f"- status: {status}"]
    if duration_s is not None:
        lines.append(f"- duration_s: {duration_s}")
    for note in notes or []:
        lines.append(f"- {' '.join(str(note).split())}")
    lines.extend(["", "### Message", "", _fence("message", sent), ""])
    lines.extend(["### Reply", "", _fence("reply", reply), ""])
    return "\n".join(lines) + "\n"


def _fence(tag: str, text: str) -> str:
    """Wrap *text* in ``<tag>`` delimiters, neutralising a literal closer."""
    body = (text or "").strip() or f"(no {tag})"
    body = body.replace(f"</{tag}>", f"<\\/{tag}>")
    return f"<{tag}>\n{body}\n</{tag}>"


def _unfence(chunk: str, tag: str) -> str:
    """The text inside the first ``<tag>`` block of *chunk*, or ``""``."""
    match = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", chunk, re.DOTALL)
    if match is None:
        return ""
    return match.group(1).replace(f"<\\/{tag}>", f"</{tag}>")


def parse_exchanges(body: str) -> list[dict[str, str]]:
    """Read the exchange sections back out of a record body.

    Returns one dict per exchange with ``number``, ``message`` and ``reply``.
    Used when a resume fails and the follow-up has to quote the line so far.
    """
    out: list[dict[str, str]] = []
    matches = list(_EXCHANGE_RE.finditer(body or ""))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[match.start() : end]
        out.append(
            {
                "number": match.group(1),
                "message": _unfence(chunk, "message"),
                "reply": _unfence(chunk, "reply"),
            }
        )
    return out


def append_exchange(path: Path, meta_updates: dict[str, Any], section: str) -> dict[str, Any]:
    """Append *section* to the record at *path* and merge *meta_updates*.

    Returns the record's new frontmatter. The body is appended to rather than
    rewritten, so an exchange already on disk cannot be lost to a later write.
    """
    meta, body = read_record(path)
    merged = {**meta, **meta_updates}
    new_body = body.rstrip("\n") + "\n\n" + section if body.strip() else section
    merged["exchanges"] = str(exchange_count(new_body))
    write_record(path, merged, new_body)
    return merged
