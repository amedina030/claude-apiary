#!/usr/bin/env python3
"""Tests for core.apiary_profiles — extends chain, deep merge, $replace, errors."""

import tempfile
import unittest
from pathlib import Path

from core.apiary_profiles import (
    SUPPORTED_SCHEMA_VERSION,
    ProfileCycleError,
    ProfileMergeError,
    ProfileNotFoundError,
    ProfileSchemaError,
    list_available,
    resolve,
)
from core.utils.jsonc import JsoncParseError


def _write(profiles_dir: Path, name: str, body: str) -> Path:
    path = profiles_dir / f"{name}.jsonc"
    path.write_text(body, encoding="utf-8")
    return path


class _ProfilesDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.profiles = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()


class TestResolveBasic(_ProfilesDir):
    def test_single_profile_no_extends(self):
        _write(self.profiles, "solo", '{"$schema_version": 1, "x": 1}')
        result = resolve("solo", self.profiles)
        self.assertEqual(result.name, "solo")
        self.assertEqual(result.profiles_applied, ["solo"])
        self.assertEqual(result.merged, {"x": 1})
        self.assertIn("solo", result.content_hashes)
        self.assertTrue(result.content_hashes["solo"].startswith("sha256:"))

    def test_extends_single_parent(self):
        _write(self.profiles, "base", '{"$schema_version": 1, "a": 1, "b": 2}')
        _write(
            self.profiles, "child", '{"$schema_version": 1, "extends": ["base"], "b": 99, "c": 3}'
        )
        result = resolve("child", self.profiles)
        self.assertEqual(result.profiles_applied, ["base", "child"])
        self.assertEqual(result.merged, {"a": 1, "b": 99, "c": 3})
        self.assertIn("base", result.content_hashes)
        self.assertIn("child", result.content_hashes)

    def test_extends_chain_depth_three(self):
        _write(self.profiles, "a", '{"$schema_version": 1, "level": "a"}')
        _write(self.profiles, "b", '{"$schema_version": 1, "extends": ["a"], "level": "b"}')
        _write(self.profiles, "c", '{"$schema_version": 1, "extends": ["b"], "level": "c"}')
        result = resolve("c", self.profiles)
        self.assertEqual(result.profiles_applied, ["a", "b", "c"])
        self.assertEqual(result.merged, {"level": "c"})

    def test_multiple_extends_left_to_right(self):
        _write(self.profiles, "p1", '{"$schema_version": 1, "x": 1, "shared": "p1"}')
        _write(self.profiles, "p2", '{"$schema_version": 1, "y": 2, "shared": "p2"}')
        _write(self.profiles, "c", '{"$schema_version": 1, "extends": ["p1", "p2"], "z": 3}')
        result = resolve("c", self.profiles)
        self.assertEqual(result.merged["x"], 1)
        self.assertEqual(result.merged["y"], 2)
        self.assertEqual(result.merged["z"], 3)
        self.assertEqual(result.merged["shared"], "p2")
        self.assertEqual(result.profiles_applied, ["p1", "p2", "c"])


class TestDeepMerge(_ProfilesDir):
    def test_dicts_merge_recursively(self):
        _write(self.profiles, "base", '{"$schema_version": 1, "permissions": {"allow": ["A"]}}')
        _write(
            self.profiles,
            "child",
            '{"$schema_version": 1, "extends": ["base"], "permissions": {"deny": ["B"]}}',
        )
        result = resolve("child", self.profiles)
        self.assertEqual(result.merged["permissions"], {"allow": ["A"], "deny": ["B"]})

    def test_lists_concatenate(self):
        _write(
            self.profiles,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["A", "B"]}}',
        )
        _write(
            self.profiles,
            "child",
            '{"$schema_version": 1, "extends": ["base"], "permissions": {"allow": ["C"]}}',
        )
        result = resolve("child", self.profiles)
        self.assertEqual(result.merged["permissions"]["allow"], ["A", "B", "C"])

    def test_scalar_overlay_wins(self):
        _write(self.profiles, "base", '{"$schema_version": 1, "name": "base"}')
        _write(
            self.profiles,
            "child",
            '{"$schema_version": 1, "extends": ["base"], "name": "child"}',
        )
        result = resolve("child", self.profiles)
        self.assertEqual(result.merged["name"], "child")


class TestReplaceWrapper(_ProfilesDir):
    def test_replace_replaces_list(self):
        _write(
            self.profiles,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["A", "B"]}}',
        )
        _write(
            self.profiles,
            "child",
            '{"$schema_version": 1, "extends": ["base"], "permissions": {"allow": {"$replace": ["ONLY"]}}}',
        )
        result = resolve("child", self.profiles)
        self.assertEqual(result.merged["permissions"]["allow"], ["ONLY"])

    def test_replace_replaces_dict(self):
        _write(
            self.profiles,
            "base",
            '{"$schema_version": 1, "permissions": {"allow": ["A"], "deny": ["X"]}}',
        )
        _write(
            self.profiles,
            "child",
            '{"$schema_version": 1, "extends": ["base"], "permissions": {"$replace": {"allow": ["only"]}}}',
        )
        result = resolve("child", self.profiles)
        self.assertEqual(result.merged["permissions"], {"allow": ["only"]})

    def test_replace_wrapper_is_stripped_from_output(self):
        _write(
            self.profiles,
            "solo",
            '{"$schema_version": 1, "permissions": {"allow": {"$replace": ["X"]}}}',
        )
        result = resolve("solo", self.profiles)
        self.assertEqual(result.merged, {"permissions": {"allow": ["X"]}})

    def test_replace_with_extra_keys_raises(self):
        _write(
            self.profiles,
            "solo",
            '{"$schema_version": 1, "permissions": {"allow": {"$replace": [1], "extra": 2}}}',
        )
        with self.assertRaises(ProfileMergeError):
            resolve("solo", self.profiles)


class TestErrors(_ProfilesDir):
    def test_missing_profile(self):
        _write(self.profiles, "exists", '{"$schema_version": 1}')
        with self.assertRaises(ProfileNotFoundError) as cm:
            resolve("ghost", self.profiles)
        self.assertIn("exists", cm.exception.available)
        self.assertEqual(cm.exception.name, "ghost")

    def test_cycle_direct(self):
        _write(self.profiles, "a", '{"$schema_version": 1, "extends": ["a"]}')
        with self.assertRaises(ProfileCycleError) as cm:
            resolve("a", self.profiles)
        self.assertEqual(cm.exception.cycle_path, ["a", "a"])

    def test_cycle_indirect(self):
        _write(self.profiles, "a", '{"$schema_version": 1, "extends": ["b"]}')
        _write(self.profiles, "b", '{"$schema_version": 1, "extends": ["a"]}')
        with self.assertRaises(ProfileCycleError) as cm:
            resolve("a", self.profiles)
        self.assertEqual(cm.exception.cycle_path, ["a", "b", "a"])

    def test_missing_schema_version(self):
        _write(self.profiles, "x", '{"x": 1}')
        with self.assertRaises(ProfileSchemaError) as cm:
            resolve("x", self.profiles)
        self.assertIn("$schema_version", str(cm.exception))

    def test_unsupported_schema_version(self):
        _write(self.profiles, "x", '{"$schema_version": 999}')
        with self.assertRaises(ProfileSchemaError) as cm:
            resolve("x", self.profiles)
        self.assertIn("999", str(cm.exception))
        self.assertIn("Upgrade apiary", str(cm.exception))

    def test_schema_version_wrong_type(self):
        _write(self.profiles, "x", '{"$schema_version": "1"}')
        with self.assertRaises(ProfileSchemaError):
            resolve("x", self.profiles)

    def test_extends_wrong_type(self):
        _write(self.profiles, "x", '{"$schema_version": 1, "extends": "base"}')
        with self.assertRaises(ProfileSchemaError):
            resolve("x", self.profiles)

    def test_jsonc_parse_error_bubbles(self):
        _write(self.profiles, "broken", '{"$schema_version": 1, "x": ,}')
        with self.assertRaises(JsoncParseError):
            resolve("broken", self.profiles)

    def test_profile_not_a_dict(self):
        _write(self.profiles, "x", "[1, 2, 3]")
        with self.assertRaises(ProfileSchemaError):
            resolve("x", self.profiles)


class TestListAvailable(_ProfilesDir):
    def test_lists_only_jsonc(self):
        _write(self.profiles, "a", '{"$schema_version": 1}')
        _write(self.profiles, "b", '{"$schema_version": 1}')
        (self.profiles / "not-a-profile.txt").write_text("", encoding="utf-8")
        self.assertEqual(list_available(self.profiles), ["a", "b"])

    def test_empty_when_missing(self):
        self.assertEqual(list_available(self.profiles / "nonexistent"), [])


class TestContentHashes(_ProfilesDir):
    def test_hashes_cover_all_applied_profiles(self):
        _write(self.profiles, "base", '{"$schema_version": 1}')
        _write(self.profiles, "mid", '{"$schema_version": 1, "extends": ["base"]}')
        _write(self.profiles, "top", '{"$schema_version": 1, "extends": ["mid"]}')
        result = resolve("top", self.profiles)
        self.assertEqual(set(result.content_hashes.keys()), {"base", "mid", "top"})
        for h in result.content_hashes.values():
            self.assertTrue(h.startswith("sha256:"))
            self.assertEqual(len(h), len("sha256:") + 64)


class TestSchemaVersionConstant(unittest.TestCase):
    def test_current_version_is_one(self):
        self.assertEqual(SUPPORTED_SCHEMA_VERSION, 1)


if __name__ == "__main__":
    unittest.main()
