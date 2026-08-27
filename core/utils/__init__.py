"""Shared helpers. If you are about to write one, look here first.

The rule (review §5a-C): these names are defined **once**, in this
package, and every subsystem imports them. Adding a second ``git_root``
or a ninth hand-rolled ``tmp + os.replace`` is the finding this package
exists to close.

===========================  ==========================================
``git_root(start)``          ``core.utils.gitutil`` — repo root, or None
``read_json_object(path)``   ``core.utils.jsonio``  — dict, or None
``write_text_atomic(...)``   ``core.utils.atomic``  — tmp + os.replace
``write_json_atomic(...)``   ``core.utils.atomic``
``now_iso()``                ``core.utils.timeutil`` — the one format
``parse_iso(ts)``            ``core.utils.timeutil`` — datetime, or None
``FileLock``                 ``core.utils.filelock``
``get_project_key(repo)``    ``core.utils.project``
``resolve_state_dir(...)``   ``core.utils.state``   — per-target state
``MAIN_APIARY_UID``          ``core.utils.state``
===========================  ==========================================

The five leaf helpers are re-exported here so ``from core.utils import
git_root`` works. ``core.utils.state`` is deliberately *not* re-exported:
it pulls in the registry, the file lock and a git subprocess, and hooks
import this package on the tool-call hot path.
"""
from core.utils.atomic import write_json_atomic, write_text_atomic
from core.utils.gitutil import git_root, main_worktree_root
from core.utils.jsonio import read_json_object
from core.utils.timeutil import ISO_FORMAT, now_iso, parse_iso

__all__ = [
    "ISO_FORMAT",
    "git_root",
    "main_worktree_root",
    "now_iso",
    "parse_iso",
    "read_json_object",
    "write_json_atomic",
    "write_text_atomic",
]
