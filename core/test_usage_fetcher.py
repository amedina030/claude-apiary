"""Unit tests for core.usage_fetcher."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from core import usage_fetcher


class ReadAccessTokenTests(unittest.TestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / "no-such.json"
            self.assertIsNone(usage_fetcher._read_access_token(p))

    def test_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / "c.json"
            p.write_text("{ not json", encoding="utf-8")
            self.assertIsNone(usage_fetcher._read_access_token(p))

    def test_missing_oauth_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / "c.json"
            p.write_text(json.dumps({"other": 1}), encoding="utf-8")
            self.assertIsNone(usage_fetcher._read_access_token(p))

    def test_empty_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / "c.json"
            p.write_text(json.dumps({"claudeAiOauth": {"accessToken": ""}}), encoding="utf-8")
            self.assertIsNone(usage_fetcher._read_access_token(p))

    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / "c.json"
            p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "abc"}}), encoding="utf-8")
            self.assertEqual(usage_fetcher._read_access_token(p), "abc")


class FetchUsageTests(unittest.TestCase):
    def _write_creds(self, tmp: str, token: str = "tok") -> Path:
        p = Path(tmp).resolve() / "c.json"
        p.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}), encoding="utf-8")
        return p

    def test_no_token_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / "missing.json"
            self.assertIsNone(usage_fetcher.fetch_usage(p))

    def test_happy_path(self):
        payload = {
            "five_hour": {"utilization": 4.0, "resets_at": "2026-04-21T09:00:00+00:00"},
            "seven_day": {"utilization": 42.0, "resets_at": "2026-04-23T19:59:59+00:00"},
        }

        class FakeResp:
            def read(self_inner):
                return json.dumps(payload).encode("utf-8")

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            creds = self._write_creds(tmp)
            with mock.patch("core.usage_fetcher.urllib.request.urlopen", return_value=FakeResp()):
                got = usage_fetcher.fetch_usage(creds)
        self.assertEqual(got, payload)

    def test_http_error_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            creds = self._write_creds(tmp)
            err = urllib.error.HTTPError(
                usage_fetcher.USAGE_URL, 429, "Too Many Requests", {}, None
            )
            with mock.patch("core.usage_fetcher.urllib.request.urlopen", side_effect=err):
                self.assertIsNone(usage_fetcher.fetch_usage(creds))

    def test_timeout_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            creds = self._write_creds(tmp)
            with mock.patch(
                "core.usage_fetcher.urllib.request.urlopen", side_effect=TimeoutError("slow")
            ):
                self.assertIsNone(usage_fetcher.fetch_usage(creds))

    def test_bad_json_returns_none(self):
        class FakeResp:
            def read(self_inner):
                return b"not json"

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            creds = self._write_creds(tmp)
            with mock.patch("core.usage_fetcher.urllib.request.urlopen", return_value=FakeResp()):
                self.assertIsNone(usage_fetcher.fetch_usage(creds))


if __name__ == "__main__":
    unittest.main()
