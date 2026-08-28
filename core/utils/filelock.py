"""Cross-platform file lock using OS-level locking."""

import os
import time
from pathlib import Path


class FileLock:
    """Context manager that acquires an OS-level file lock via a sidecar .lock file.

    Uses msvcrt.locking on Windows, fcntl.flock on POSIX.
    """

    def __init__(self, path, timeout=5):
        self.lock_path = Path(str(path) + ".lock")
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "w", encoding="utf-8")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, IOError):
                if time.monotonic() >= deadline:
                    self._fh.close()
                    raise TimeoutError(f"Could not acquire lock: {self.lock_path}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
        return False
