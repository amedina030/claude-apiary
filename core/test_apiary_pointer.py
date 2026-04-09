#!/usr/bin/env python3
"""Tests for core.utils.apiary_pointer (todo #263, decision #269).

The pointer file lets hooks locate the apiary code repo regardless of
which repo the Claude Code session was started in.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.utils import apiary_pointer  # noqa: E402


class WritePointerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.pointer_path = self.tmp_path / "claude" / "apiary.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_json_payload_with_absolute_path(self):
        fake_repo = self.tmp_path / "some" / "repo"
        fake_repo.mkdir(parents=True)
        returned = apiary_pointer.write_pointer(
            fake_repo, pointer_path=self.pointer_path
        )
        self.assertEqual(returned, self.pointer_path)
        self.assertTrue(self.pointer_path.is_file())
        payload = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], apiary_pointer.POINTER_VERSION)
        self.assertEqual(Path(payload["repo_path"]), fake_repo.resolve())

    def test_creates_parent_directory(self):
        fake_repo = self.tmp_path / "repo"
        fake_repo.mkdir()
        self.assertFalse(self.pointer_path.parent.exists())
        apiary_pointer.write_pointer(fake_repo, pointer_path=self.pointer_path)
        self.assertTrue(self.pointer_path.parent.is_dir())

    def test_overwrites_existing_file_atomically(self):
        fake_repo = self.tmp_path / "repo"
        fake_repo.mkdir()
        self.pointer_path.parent.mkdir(parents=True)
        self.pointer_path.write_text("garbage", encoding="utf-8")
        apiary_pointer.write_pointer(fake_repo, pointer_path=self.pointer_path)
        # No stray .tmp file left behind
        tmp_sibling = self.pointer_path.with_name(self.pointer_path.name + ".tmp")
        self.assertFalse(tmp_sibling.exists())
        payload = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        self.assertEqual(Path(payload["repo_path"]), fake_repo.resolve())

    def test_default_path_used_when_none_passed(self):
        fake_repo = self.tmp_path / "repo"
        fake_repo.mkdir()
        custom_default = self.tmp_path / "claude" / "apiary.json"
        with mock.patch.object(
            apiary_pointer, "DEFAULT_POINTER_PATH", custom_default
        ):
            returned = apiary_pointer.write_pointer(fake_repo)
            self.assertEqual(returned, custom_default)
            self.assertTrue(custom_default.is_file())


class ReadPointerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.pointer_path = self.tmp_path / "apiary.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_raw(self, text: str):
        self.pointer_path.write_text(text, encoding="utf-8")

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            apiary_pointer.read_pointer(pointer_path=self.pointer_path)
        )

    def test_valid_payload_returns_dict(self):
        fake_repo = self.tmp_path / "repo"
        fake_repo.mkdir()
        apiary_pointer.write_pointer(fake_repo, pointer_path=self.pointer_path)
        payload = apiary_pointer.read_pointer(pointer_path=self.pointer_path)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["version"], apiary_pointer.POINTER_VERSION)
        self.assertEqual(Path(payload["repo_path"]), fake_repo.resolve())

    def test_malformed_json_returns_none(self):
        self._write_raw("{not json")
        self.assertIsNone(
            apiary_pointer.read_pointer(pointer_path=self.pointer_path)
        )

    def test_non_object_payload_returns_none(self):
        self._write_raw('["list", "not object"]')
        self.assertIsNone(
            apiary_pointer.read_pointer(pointer_path=self.pointer_path)
        )

    def test_missing_repo_path_returns_none(self):
        self._write_raw('{"version": 1}')
        self.assertIsNone(
            apiary_pointer.read_pointer(pointer_path=self.pointer_path)
        )

    def test_empty_repo_path_returns_none(self):
        self._write_raw('{"version": 1, "repo_path": ""}')
        self.assertIsNone(
            apiary_pointer.read_pointer(pointer_path=self.pointer_path)
        )


class GetRepoPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.pointer_path = self.tmp_path / "apiary.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_path_when_repo_exists(self):
        fake_repo = self.tmp_path / "repo"
        fake_repo.mkdir()
        apiary_pointer.write_pointer(fake_repo, pointer_path=self.pointer_path)
        result = apiary_pointer.get_repo_path(pointer_path=self.pointer_path)
        self.assertEqual(result, fake_repo)

    def test_returns_none_when_repo_dir_missing(self):
        self.pointer_path.write_text(
            json.dumps({"version": 1, "repo_path": "/path/that/does/not/exist/12345"}),
            encoding="utf-8",
        )
        self.assertIsNone(
            apiary_pointer.get_repo_path(pointer_path=self.pointer_path)
        )

    def test_returns_none_when_repo_path_is_a_file(self):
        fake_file = self.tmp_path / "not_a_dir"
        fake_file.write_text("x", encoding="utf-8")
        self.pointer_path.write_text(
            json.dumps({"version": 1, "repo_path": str(fake_file)}),
            encoding="utf-8",
        )
        self.assertIsNone(
            apiary_pointer.get_repo_path(pointer_path=self.pointer_path)
        )


class RemovePointerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.pointer_path = self.tmp_path / "apiary.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_false_when_missing(self):
        self.assertFalse(
            apiary_pointer.remove_pointer(pointer_path=self.pointer_path)
        )

    def test_removes_existing_file(self):
        self.pointer_path.write_text("{}", encoding="utf-8")
        self.assertTrue(
            apiary_pointer.remove_pointer(pointer_path=self.pointer_path)
        )
        self.assertFalse(self.pointer_path.exists())

    def test_idempotent(self):
        self.pointer_path.write_text("{}", encoding="utf-8")
        apiary_pointer.remove_pointer(pointer_path=self.pointer_path)
        self.assertFalse(
            apiary_pointer.remove_pointer(pointer_path=self.pointer_path)
        )


if __name__ == "__main__":
    unittest.main()
