"""Tests for compass.ab — the live A/B arm assignment.

Hermetic: every test points ``$APIARY_COMPASS_CONFIG`` and
``$APIARY_TARGET_STATE_DIR`` at a temp dir, so nothing reads the shipped
config or the user's real session identity files.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass import ab  # noqa: E402
from core.utils.state import TARGET_STATE_DIR_ENV  # noqa: E402


class EnvSandbox(unittest.TestCase):
    """Base class: a temp state dir and a temp config, restored on teardown."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        (self.state / "sessions").mkdir(parents=True)
        self._set_env(TARGET_STATE_DIR_ENV, str(self.state))

    def _set_env(self, key: str, value: str | None) -> None:
        previous = os.environ.get(key)

        def restore():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

        self.addCleanup(restore)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def write_config(self, **overrides) -> Path:
        config = dict(ab.DEFAULT_CONFIG)
        config.update(overrides)
        path = self.root / "compass-config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self._set_env(ab.CONFIG_ENV, str(path))
        return path

    def write_identity(self, session_short: str, **fields) -> Path:
        path = self.state / "sessions" / f"identity-{session_short}.json"
        payload = {"role": "user", "mission": "general", "registered": True}
        payload.update(fields)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class LoadConfigTests(EnvSandbox):
    def test_shipped_default_is_ab_disabled(self):
        self._set_env(ab.CONFIG_ENV, None)
        self.assertFalse(ab.load_config()["ab_enabled"],
                         "compass/config.json must ship with the A/B off")

    def test_reads_override_file(self):
        self.write_config(ab_enabled=True, ab_seed="seed-x", ab_on_fraction=0.25)
        config = ab.load_config()
        self.assertTrue(config["ab_enabled"])
        self.assertEqual(config["ab_seed"], "seed-x")
        self.assertEqual(config["ab_on_fraction"], 0.25)

    def test_malformed_config_falls_back_to_defaults(self):
        path = self.root / "broken.json"
        path.write_text("{not json,", encoding="utf-8")
        self._set_env(ab.CONFIG_ENV, str(path))
        self.assertEqual(ab.load_config(), ab.DEFAULT_CONFIG)

    def test_missing_config_falls_back_to_defaults(self):
        self._set_env(ab.CONFIG_ENV, str(self.root / "nope.json"))
        self.assertEqual(ab.load_config(), ab.DEFAULT_CONFIG)

    def test_unknown_keys_are_ignored(self):
        path = self.root / "extra.json"
        path.write_text(json.dumps({"ab_enabled": True, "nonsense": 1}), encoding="utf-8")
        self._set_env(ab.CONFIG_ENV, str(path))
        self.assertEqual(set(ab.load_config()), set(ab.DEFAULT_CONFIG))


class AssignArmTests(EnvSandbox):
    def test_deterministic_for_the_same_id_and_seed(self):
        config = {"ab_enabled": True, "ab_seed": "s", "ab_on_fraction": 0.5}
        first = ab.assign_arm("abcd1234", config)
        for _ in range(5):
            self.assertEqual(ab.assign_arm("abcd1234", config), first)

    def test_full_uuid_and_prefix_agree(self):
        config = {"ab_enabled": True, "ab_seed": "s", "ab_on_fraction": 0.5}
        self.assertEqual(
            ab.assign_arm("abcd1234-1111-2222-3333-444455556666", config),
            ab.assign_arm("ABCD1234", config),
        )

    def test_seed_change_reshuffles(self):
        ids = [f"{i:08x}" for i in range(200)]
        a = [ab.assign_arm(i, {"ab_seed": "one", "ab_on_fraction": 0.5}) for i in ids]
        b = [ab.assign_arm(i, {"ab_seed": "two", "ab_on_fraction": 0.5}) for i in ids]
        self.assertNotEqual(a, b)

    def test_split_is_roughly_balanced(self):
        config = {"ab_seed": "balance", "ab_on_fraction": 0.5}
        arms = [ab.assign_arm(f"{i:08x}", config) for i in range(1000)]
        on = arms.count(ab.ARM_ON)
        self.assertTrue(400 < on < 600, f"expected a near-even split, got on={on}/1000")

    def test_fraction_zero_and_one_are_absolute(self):
        ids = [f"{i:08x}" for i in range(50)]
        self.assertTrue(all(ab.assign_arm(i, {"ab_seed": "s", "ab_on_fraction": 0}) == ab.ARM_OFF
                            for i in ids))
        self.assertTrue(all(ab.assign_arm(i, {"ab_seed": "s", "ab_on_fraction": 1}) == ab.ARM_ON
                            for i in ids))

    def test_nonsense_fraction_falls_back_to_half(self):
        config = {"ab_seed": "s", "ab_on_fraction": "banana"}
        arms = {ab.assign_arm(f"{i:08x}", config) for i in range(50)}
        self.assertEqual(arms, set(ab.ARMS))


class ArmForSessionTests(EnvSandbox):
    def test_disabled_means_everyone_is_on(self):
        self.write_config(ab_enabled=False, ab_on_fraction=0.0)
        arms = {ab.arm_for_session(f"{i:08x}") for i in range(50)}
        self.assertEqual(arms, {ab.ARM_ON},
                         "with the experiment off nothing may change for the user")

    def test_enabled_produces_both_arms(self):
        self.write_config(ab_enabled=True)
        arms = {ab.arm_for_session(f"{i:08x}") for i in range(100)}
        self.assertEqual(arms, set(ab.ARMS))

    def test_recorded_arm_wins_over_a_seed_change(self):
        self.write_identity("abcd1234", compass_arm=ab.ARM_OFF)
        self.write_config(ab_enabled=True, ab_on_fraction=1.0)  # flip would say "on"
        self.assertEqual(ab.arm_for_session("abcd1234"), ab.ARM_OFF)

    def test_bad_recorded_value_is_ignored(self):
        self.write_identity("abcd1234", compass_arm="sideways")
        self.write_config(ab_enabled=True, ab_on_fraction=1.0)
        self.assertEqual(ab.arm_for_session("abcd1234"), ab.ARM_ON)

    def test_empty_session_id_is_on(self):
        self.write_config(ab_enabled=True, ab_on_fraction=0.0)
        self.assertEqual(ab.arm_for_session(""), ab.ARM_ON)

    def test_arm_for_new_session_ignores_the_record(self):
        self.write_identity("abcd1234", compass_arm=ab.ARM_OFF)
        self.write_config(ab_enabled=True, ab_on_fraction=1.0)
        self.assertEqual(ab.arm_for_new_session("abcd1234"), ab.ARM_ON)

    def test_arm_for_new_session_is_on_while_disabled(self):
        self.write_config(ab_enabled=False, ab_on_fraction=0.0)
        self.assertEqual(ab.arm_for_new_session("abcd1234"), ab.ARM_ON)


class RunInitRecordsArmTests(EnvSandbox):
    """run_init must stamp the arm so a later seed change cannot rewrite it."""

    def test_identity_file_carries_the_arm(self):
        from core.session import load_identity
        from core.startup import run_init

        self.write_config(ab_enabled=True, ab_on_fraction=0.0)  # everyone off
        sid = "abcd1234-1111-2222-3333-444455556666"
        run_init(sid, "", str(self.root))

        path = self.state / "sessions" / "identity-abcd1234.json"
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["compass_arm"],
                         ab.ARM_OFF)
        self.assertEqual(load_identity("abcd1234")["compass_arm"], ab.ARM_OFF)

    def test_arm_defaults_to_on_when_disabled(self):
        from core.startup import run_init
        self.write_config(ab_enabled=False)
        run_init("beef0001-1111-2222-3333-444455556666", "", str(self.root))
        path = self.state / "sessions" / "identity-beef0001.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["compass_arm"],
                         ab.ARM_ON)

    def test_load_identity_defaults_arm_to_none_when_absent(self):
        from core.session import load_identity
        self.write_identity("cafe0001")  # no compass_arm key
        self.assertIsNone(load_identity("cafe0001")["compass_arm"])


class HookGuardTests(EnvSandbox):
    """The startup hook only skips injection for a recorded/assigned 'off'."""

    def test_injects_while_the_experiment_is_disabled(self):
        from core.hooks import startup_prompt_hook as hook
        from core.session import SessionId
        self.write_config(ab_enabled=False, ab_on_fraction=0.0)
        self.assertTrue(hook._compass_arm_on(SessionId("abcd1234")))

    def test_skips_for_the_off_arm(self):
        from core.hooks import startup_prompt_hook as hook
        from core.session import SessionId
        self.write_identity("abcd1234", compass_arm=ab.ARM_OFF)
        self.assertFalse(hook._compass_arm_on(SessionId("abcd1234")))

    def test_failure_means_inject(self):
        from core.hooks import startup_prompt_hook as hook

        class Exploding:
            @property
            def short(self):
                raise RuntimeError("boom")

        self.assertTrue(hook._compass_arm_on(Exploding()))


if __name__ == "__main__":
    unittest.main()
