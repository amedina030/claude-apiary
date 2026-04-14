"""T-2026-131: tests for runner/schema_versions.py."""
import unittest

from runner.schema_versions import (
    EXECUTION_SCHEMA_VERSION,
    HARDEN_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    SPEC_SCHEMA_VERSION,
    SchemaVersionError,
    assert_schema_version,
)


class TestAssertSchemaVersion(unittest.TestCase):

    def test_matching_version_passes(self):
        assert_schema_version({"schema_version": 1}, "plan", 1)

    def test_mismatched_version_raises(self):
        with self.assertRaises(SchemaVersionError) as cm:
            assert_schema_version({"schema_version": 2}, "plan", 1)
        msg = str(cm.exception)
        self.assertIn("plan", msg)
        self.assertIn("schema_version=2", msg)
        self.assertIn("[1]", msg)

    def test_missing_version_raises(self):
        with self.assertRaises(SchemaVersionError) as cm:
            assert_schema_version({"uuid": "x"}, "execution", 1)
        self.assertIn("schema_version=None", str(cm.exception))

    def test_non_dict_raises(self):
        with self.assertRaises(SchemaVersionError):
            assert_schema_version([1, 2, 3], "harden", 1)

    def test_set_of_supported_versions(self):
        assert_schema_version({"schema_version": 1}, "plan", {1, 2})
        assert_schema_version({"schema_version": 2}, "plan", {1, 2})
        with self.assertRaises(SchemaVersionError):
            assert_schema_version({"schema_version": 3}, "plan", {1, 2})

    def test_error_is_value_error_subclass(self):
        # Consumers catch ValueError; SchemaVersionError must subclass it.
        try:
            assert_schema_version({"schema_version": 99}, "spec", 1)
        except ValueError:
            pass
        else:
            self.fail("expected ValueError")


class TestVersionConstants(unittest.TestCase):

    def test_all_constants_are_positive_ints(self):
        # Sanity: schemas start at 1 and increment by integers.
        for v in (SPEC_SCHEMA_VERSION, PLAN_SCHEMA_VERSION,
                  EXECUTION_SCHEMA_VERSION, HARDEN_SCHEMA_VERSION,
                  REPORT_SCHEMA_VERSION):
            self.assertIsInstance(v, int)
            self.assertGreaterEqual(v, 1)


if __name__ == "__main__":
    unittest.main()
