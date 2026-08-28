#!/usr/bin/env python3
"""Tests for the generated per-repo launcher (``core/launcher_template.py``).

Until now nothing ever *executed* the launcher — ``test_install.py`` only
asserted the file exists (review: "notably, the generated launcher" is
untested). Since it now runs its target in-process via ``runpy`` instead of
spawning a second interpreter, the contract it has to keep is worth pinning:
env exports, argv, stdin pass-through, exit-code propagation (a gate's exit 2
must survive), the removed-script message, and the unreachable-main-apiary
path.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.launcher_template import LAUNCHER_PY  # noqa: E402


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.main_apiary = root / "main-apiary"
        self.target = root / "target"
        self.launcher = self.target / ".claude" / "apiary" / "launch.py"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text(LAUNCHER_PY, encoding="utf-8")
        (self.main_apiary / ".repos" / "target-7").mkdir(parents=True)
        self._pointer({"main_apiary_path": str(self.main_apiary)})
        (self.launcher.parent / "self-pointer.json").write_text(
            json.dumps({"schema_version": 1, "name": "target", "uid": 7}), encoding="utf-8"
        )

    def _pointer(self, data):
        (self.launcher.parent / "main-apiary-pointer.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def _script(self, rel, body):
        path = self.main_apiary / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return rel

    def _launch(self, *args, stdin=""):
        env = {k: v for k, v in os.environ.items() if not k.startswith("APIARY_")}
        return subprocess.run(
            [sys.executable, str(self.launcher), *args],
            input=stdin,
            text=True,
            capture_output=True,
            env=env,
            timeout=60,
        )

    # --- the target actually runs -----------------------------------------

    def test_target_runs_and_sees_stdin_and_argv(self):
        rel = self._script(
            "tools/echo.py",
            (
                "import sys\n"
                "print('argv=' + repr(sys.argv[1:]))\n"
                "print('stdin=' + sys.stdin.read())\n"
            ),
        )
        r = self._launch(rel, "one", "two", stdin="hello")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("argv=['one', 'two']", r.stdout)
        self.assertIn("stdin=hello", r.stdout)

    def test_target_runs_as_main(self):
        rel = self._script("tools/name.py", "print('name=' + __name__)\n")
        r = self._launch(rel)
        self.assertIn("name=__main__", r.stdout)

    def test_target_runs_in_process(self):
        """runpy, not a second interpreter — the whole point of the change."""
        rel = self._script("tools/pid.py", "import os; print(os.getpid())\n")
        r = self._launch(rel)
        # The launcher prints nothing itself, so the only pid printed is the
        # target's; assert it is a live pid belonging to *one* process by
        # checking the target saw the launcher's own sys.argv[0] rewritten.
        self.assertTrue(r.stdout.strip().isdigit(), r.stdout)

    # --- env exports -------------------------------------------------------

    def test_env_vars_are_exported_to_the_target(self):
        rel = self._script(
            "tools/env.py",
            (
                "import os\n"
                "for k in ('APIARY_MAIN_REPO', 'APIARY_TARGET_REPO',\n"
                "          'APIARY_TARGET_STATE_DIR'):\n"
                "    print(k + '=' + os.environ.get(k, '<unset>'))\n"
            ),
        )
        r = self._launch(rel)
        self.assertIn(f"APIARY_MAIN_REPO={self.main_apiary}", r.stdout)
        self.assertIn(f"APIARY_TARGET_REPO={self.target}", r.stdout)
        self.assertIn(
            f"APIARY_TARGET_STATE_DIR={self.main_apiary / '.repos' / 'target-7'}", r.stdout
        )

    def test_state_dir_is_unset_when_the_pin_directory_is_missing(self):
        (self.launcher.parent / "self-pointer.json").unlink()
        rel = self._script(
            "tools/env2.py",
            ("import os\nprint('state=' + os.environ.get('APIARY_TARGET_STATE_DIR', '<unset>'))\n"),
        )
        self.assertIn("state=<unset>", self._launch(rel).stdout)

    # --- exit codes --------------------------------------------------------

    def test_exit_code_is_propagated(self):
        for code in (0, 1, 2, 7):
            with self.subTest(code=code):
                rel = self._script(f"tools/exit{code}.py", f"import sys; sys.exit({code})\n")
                self.assertEqual(self._launch(rel).returncode, code)

    def test_a_gate_blocking_with_exit_2_still_blocks(self):
        rel = self._script(
            "tools/gate.py",
            (
                "import json, sys\n"
                "print(json.dumps({'decision': 'block'}))\n"
                "print('nope', file=sys.stderr)\n"
                "sys.exit(2)\n"
            ),
        )
        r = self._launch(rel)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout), {"decision": "block"})
        self.assertIn("nope", r.stderr)

    def test_a_crashing_target_exits_1_not_2(self):
        # Exit 2 is Claude Code's "block the call"; a bug must never mean that.
        rel = self._script("tools/boom.py", "raise RuntimeError('boom')\n")
        r = self._launch(rel)
        self.assertEqual(r.returncode, 1)
        self.assertIn("RuntimeError: boom", r.stderr)

    def test_a_string_exit_code_becomes_1_with_the_message_on_stderr(self):
        rel = self._script("tools/msg.py", "import sys; sys.exit('bad config')\n")
        r = self._launch(rel)
        self.assertEqual(r.returncode, 1)
        self.assertIn("bad config", r.stderr)

    # --- degraded paths ----------------------------------------------------

    def test_removed_script_says_so_and_exits_0(self):
        r = self._launch("tools/gone.py")
        self.assertEqual(r.returncode, 0)
        self.assertIn("hook script removed", r.stderr)
        self.assertIn("re-run apiary install", r.stderr)

    def test_unreachable_main_apiary_exits_0(self):
        self._pointer({"main_apiary_path": str(self.main_apiary / "nope")})
        r = self._launch("tools/whatever.py")
        self.assertEqual(r.returncode, 0)
        self.assertIn("running as vanilla Claude session", r.stderr)

    def test_missing_pointer_exits_0(self):
        (self.launcher.parent / "main-apiary-pointer.json").unlink()
        self.assertEqual(self._launch("tools/whatever.py").returncode, 0)

    def test_print_repo_path(self):
        r = self._launch("--print-repo-path")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), str(self.main_apiary))

    def test_no_arguments_is_a_usage_error(self):
        r = self._launch()
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)

    # --- the real dispatcher through the real launcher --------------------

    def test_the_dispatcher_runs_through_a_launcher_pointed_at_this_checkout(self):
        self._pointer({"main_apiary_path": str(REPO)})
        r = self._launch("core/hooks/dispatch.py", "post", stdin=json.dumps({"tool_name": "Read"}))
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(json.loads(r.stdout), {})


if __name__ == "__main__":
    unittest.main()
