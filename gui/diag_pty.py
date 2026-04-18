"""Diagnostic: spawn claude in pywinpty and trace what happens. NOT for prod use."""

from __future__ import annotations

import shutil
import sys
import time

from winpty import PtyProcess


def main() -> int:
    exe = shutil.which("claude")
    if not exe:
        print("claude not on PATH")
        return 1
    print(f"spawning: {exe}")
    p = PtyProcess.spawn([exe], dimensions=(40, 120))
    print("spawned. reading 4 seconds of initial output...")
    deadline = time.time() + 4.0
    buf = ""
    while time.time() < deadline:
        try:
            chunk = p.read(4096)
        except EOFError:
            break
        if chunk:
            buf += chunk
        time.sleep(0.05)
    print(f"=== INITIAL OUTPUT ({len(buf)} chars) ===")
    print(repr(buf[:2000]))
    print()
    print("typing 'hi' + Enter...")
    p.write("hi\r")
    deadline = time.time() + 6.0
    after = ""
    while time.time() < deadline:
        try:
            chunk = p.read(4096)
        except EOFError:
            break
        if chunk:
            after += chunk
        time.sleep(0.05)
    print(f"=== AFTER 'hi\\r' ({len(after)} chars) ===")
    print(repr(after[:3000]))
    p.terminate(force=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
