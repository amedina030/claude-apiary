#!/usr/bin/env python3
"""Tests for the UserPromptSubmit grant hook.

The grant is the whole authorization model, so these check both halves: a
``/telephone`` prompt writes one, and everything else does not.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.hooks import dispatch
from telephone.hooks import user_prompt

SESSION = "11111111-2222-3333-4444-555555555555"


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.flags = Path(self._tmp.name) / "session-tmp"
        self.flags.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch("core.session.session_tmp_dir", return_value=self.flags)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_hook(self, prompt, session_id=SESSION, **extra):
        payload = {"prompt": prompt, "session_id": session_id, **extra}
        return user_prompt.run(payload)

    def grants(self):
        return sorted(self.flags.glob(f"*_{user_prompt.GRANT_SUFFIX}"))

    def grant_payload(self):
        files = self.grants()
        self.assertEqual(len(files), 1, f"expected exactly one grant, found {files}")
        return json.loads(files[0].read_text(encoding="utf-8"))


class TestParseInvocation(unittest.TestCase):
    def test_recognises_answer_and_act_forms(self):
        answer = user_prompt.parse_invocation("/telephone apiary what changed in the installer")
        self.assertEqual(answer["repo"], "apiary")
        self.assertFalse(answer["act"])
        self.assertEqual(answer["message"], "what changed in the installer")

        act = user_prompt.parse_invocation("/telephone apiary act fix the installer gap")
        self.assertEqual(act["repo"], "apiary")
        self.assertTrue(act["act"])
        self.assertEqual(act["message"], "fix the installer gap")

    def test_flag_spelling_of_act_is_accepted_anywhere_in_the_arguments(self):
        parsed = user_prompt.parse_invocation("/telephone apiary fix it --act")
        self.assertTrue(parsed["act"])
        self.assertEqual(parsed["message"], "fix it")

    def test_case_and_leading_space_do_not_matter(self):
        self.assertIsNotNone(user_prompt.parse_invocation("  /Telephone apiary hello"))

    def test_a_bare_command_parses_with_no_repo(self):
        parsed = user_prompt.parse_invocation("/telephone")
        self.assertEqual(parsed["repo"], "")

    def test_other_prompts_are_not_invocations(self):
        for text in (
            "",
            "tell me about /telephone",
            "run telephone/cli.py call apiary hi",
            "/telephones apiary hi",
            "please use the /telephone command on apiary",
        ):
            self.assertIsNone(user_prompt.parse_invocation(text), text)


class TestGrantWriting(HookTestCase):
    def test_a_telephone_prompt_writes_one_grant(self):
        result = self.run_hook("/telephone spanish-citizenship what did we submit")
        payload = self.grant_payload()
        self.assertFalse(payload["act"])
        self.assertEqual(payload["repo"], "spanish-citizenship")
        self.assertEqual(payload["session_id"], SESSION)
        self.assertIn("grant", result.context)
        self.assertIsNone(result.block_reason)

    def test_the_act_keyword_is_recorded_on_the_grant(self):
        self.run_hook("/telephone apiary act fix the installer gap")
        self.assertTrue(self.grant_payload()["act"])

    def test_the_grant_is_named_for_the_session(self):
        self.run_hook("/telephone apiary hi")
        self.assertTrue(self.grants()[0].name.startswith(SESSION))

    def test_no_grant_for_any_other_prompt(self):
        for text in ("hello", "tell me about /telephone", "/prose README.md"):
            self.assertIsNone(self.run_hook(text), text)
        self.assertEqual(self.grants(), [])

    def test_no_grant_without_a_session_id(self):
        self.assertIsNone(self.run_hook("/telephone apiary hi", session_id=""))
        self.assertIsNone(self.run_hook("/telephone apiary hi", session_id="not-a-uuid"))
        self.assertEqual(self.grants(), [])

    def test_the_hook_ignores_events_that_are_not_prompt_submissions(self):
        self.assertIsNone(self.run_hook("/telephone apiary hi", hook_event_name="PreToolUse"))
        self.assertEqual(self.grants(), [])

    def test_a_second_invocation_rewrites_rather_than_multiplies_the_grant(self):
        self.run_hook("/telephone apiary act do it")
        self.run_hook("/telephone apiary just ask")
        self.assertFalse(self.grant_payload()["act"])


class TestRegistration(unittest.TestCase):
    def test_the_hook_is_registered_for_user_prompt_submit(self):
        hooks = dispatch._registry()["UserPromptSubmit"]
        names = [h.name for h in hooks]
        self.assertIn("telephone_grant", names)
        row = next(h for h in hooks if h.name == "telephone_grant")
        self.assertEqual(row.module, "telephone.hooks.user_prompt")
        self.assertTrue(callable(dispatch.load_run(row.module)))

    def test_it_runs_through_the_dispatcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            flags = Path(tmp) / "session-tmp"
            flags.mkdir()
            with mock.patch("core.session.session_tmp_dir", return_value=flags):
                result = dispatch.dispatch(
                    "UserPromptSubmit",
                    {"prompt": "/telephone apiary act go", "session_id": SESSION},
                    hooks=(dispatch.Hook("telephone_grant", "telephone.hooks.user_prompt"),),
                )
            self.assertIsNone(result.block_reason)
            written = list(flags.glob(f"*_{user_prompt.GRANT_SUFFIX}"))
            self.assertEqual(len(written), 1)

    def test_a_raising_hook_leaves_no_grant_so_act_stays_locked(self):
        # Fail-open in the dispatcher means fail-closed for telephone: no
        # grant on disk is exactly what blocks act mode.
        with tempfile.TemporaryDirectory() as tmp:
            flags = Path(tmp) / "session-tmp"
            flags.mkdir()
            with mock.patch.object(user_prompt, "write_grant", side_effect=OSError("disk full")):
                with mock.patch("core.session.session_tmp_dir", return_value=flags):
                    result = dispatch.dispatch(
                        "UserPromptSubmit",
                        {"prompt": "/telephone apiary act go", "session_id": SESSION},
                        hooks=(dispatch.Hook("telephone_grant", "telephone.hooks.user_prompt"),),
                    )
            self.assertIsNone(result.block_reason)
            self.assertEqual(list(flags.glob(f"*_{user_prompt.GRANT_SUFFIX}")), [])


if __name__ == "__main__":
    unittest.main()
