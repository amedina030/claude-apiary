#!/usr/bin/env python3
"""Pins the startup hook's context-body extraction to the shared dialect."""

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from core.hooks import startup_prompt_hook
from core.testing import hermetic_env

HOOK = REPO / "core" / "hooks" / "startup_prompt_hook.py"
CONTEXT_MD = REPO / "core" / "commands" / "apiary-context.md"


class TestContextBody(unittest.TestCase):
    def body(self, text):
        return startup_prompt_hook._context_body(text)

    def test_frontmatter_is_removed(self):
        self.assertEqual(
            self.body("---\nname: x\ndesc: y\n---\n\n# Body\n\ntext\n"),
            "# Body\n\ntext\n",
        )

    def test_document_without_frontmatter_is_unchanged(self):
        self.assertEqual(self.body("# Body\n\ntext\n"), "# Body\n\ntext\n")

    def test_body_horizontal_rules_survive(self):
        self.assertEqual(
            self.body("---\nname: x\n---\n\nA\n\n---\n\nB\n"),
            "A\n\n---\n\nB\n",
        )

    def test_unterminated_fence_returns_document_unchanged(self):
        self.assertEqual(
            self.body("---\nname: x\n\n# Body\n"),
            "---\nname: x\n\n# Body\n",
        )

    def test_malformed_block_is_still_stripped(self):
        # Proves split() is used, not parse(), which would fall back to the
        # whole document and leak the header into the injected context.
        self.assertEqual(
            self.body("---\nname: x\n  bad indent\n---\nbody\n"),
            "body\n",
        )

    def test_empty_input(self):
        self.assertEqual(self.body(""), "")

    def test_nothing_after_closing_fence(self):
        self.assertEqual(self.body("---\nname: x\n---\n"), "")
        self.assertEqual(self.body("---\nname: x\n---"), "")

    def test_crlf_document(self):
        # The lone leading \r is not stripped — same as the deleted helper.
        self.assertEqual(
            self.body("---\r\nname: x\r\n---\r\n\r\n# Body\r\n"),
            "\r\n# Body\r\n",
        )

    def test_over_long_dash_runs_are_not_fences(self):
        # INTENTIONAL divergence from the deleted `_strip_frontmatter`, which
        # treated over-long dash runs as fences: the shared dialect requires a
        # line whose rstrip() is exactly three dashes. This cannot affect
        # production because core/commands/apiary-context.md uses exact
        # three-dash fences.
        self.assertEqual(
            self.body("----\nnot really\n---\nbody\n"),
            "----\nnot really\n---\nbody\n",
        )
        self.assertEqual(
            self.body("---\nname: x\n-----\nbody\n"),
            "---\nname: x\n-----\nbody\n",
        )

    def test_indented_closing_fence_is_not_a_fence(self):
        self.assertEqual(
            self.body("---\nname: x\n  ---\nbody\n"),
            "---\nname: x\n  ---\nbody\n",
        )

    def test_trailing_whitespace_on_closing_fence_is_a_fence(self):
        self.assertEqual(
            self.body("---\nname: x\n--- \nbody\n"),
            "body\n",
        )

    def test_shipped_apiary_context_md(self):
        text = CONTEXT_MD.read_text(encoding="utf-8")
        result = self.body(text.strip())
        self.assertTrue(result.startswith("## CLI invocation"))
        self.assertNotIn("name: apiary-context", result)
        self.assertNotIn("user-invocable:", result)
        self.assertNotIn("description:", result.splitlines()[0])


class TestHookInjectsStrippedRules(unittest.TestCase):
    def _purge_session_flags(self, sid: str) -> None:
        for f in (REPO / ".claude" / "apiary" / "session-tmp").glob(f"{sid}_*"):
            f.unlink(missing_ok=True)

    def test_injected_rules_block_has_no_skill_header(self):
        # A unique id per run: the hook's run-once flag lives in the REAL
        # repo's .claude/apiary/session-tmp keyed by session id (env-blind),
        # so a fixed id collides with a flag left by any earlier run and the
        # hook emits {} forever after. Exactly that happened on the runner's
        # first execution (2026-08-31): a flag from 08-26 predated this test.
        sid = str(uuid.uuid4())
        self.addCleanup(self._purge_session_flags, sid)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            payload = {
                "session_id": sid,
                "message": "hi",
                "cwd": str(home),
            }
            env = hermetic_env(
                HOME=str(home),
                USERPROFILE=str(home),
                APIARY_GUI_SESSION="",
            )
            result = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("apiary toolkit rules", result.stdout)
            self.assertIn("git rev-parse --show-toplevel", result.stdout)
            self.assertNotIn("user-invocable:", result.stdout)
            self.assertNotIn("name: apiary-context", result.stdout)


if __name__ == "__main__":
    unittest.main()
