"""Tests for ``core/install.py`` — per-repo apiary install."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import git_hooks
from core import install as install_mod
# The fake main-apiary and the throwaway git repos come from one place now —
# see core/testing.py for why they are built the way they are.
from core.testing import init_git_repo as _git_init
from core.testing import make_fake_apiary as _make_fake_apiary
from core.utils import state


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_fake_apiary(self.root)
        self.target = _git_init(self.root / "demo")

    def test_first_install_writes_all_pin_files(self):
        result = install_mod.install(self.target, apiary_repo=self.apiary)
        pin = self.target / ".claude" / "apiary"
        for name in ("launch.py", "main-apiary-pointer.json", "self-pointer.json", "version.json"):
            self.assertTrue((pin / name).is_file(), f"missing {name}")
        for name in ("flags", "session-tmp"):
            self.assertTrue((pin / name).is_dir(), f"missing {name}/")
        self.assertTrue(result.is_first_install)
        self.assertEqual(result.uid, 1)

    def test_settings_json_hooks_use_per_repo_launcher(self):
        install_mod.install(self.target, apiary_repo=self.apiary)
        settings = json.loads(
            (self.target / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        blob = json.dumps(settings.get("hooks", {}))
        self.assertIn("$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py", blob)
        # Hook events should be present
        self.assertGreater(len(settings.get("hooks", {})), 0)

    def test_slash_commands_are_copied(self):
        install_mod.install(self.target, apiary_repo=self.apiary)
        cmds = list((self.target / ".claude" / "commands").glob("*.md"))
        self.assertGreater(len(cmds), 0)

    def test_claude_md_zone_is_written(self):
        install_mod.install(self.target, apiary_repo=self.apiary)
        text = (self.target / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("<!-- apiary-context-rules-start -->", text)
        self.assertIn("<!-- apiary-context-rules-end -->", text)

    def test_gitignore_gets_dot_claude_entry(self):
        # Pre-existing .gitignore with unrelated entries
        gi = self.target / ".gitignore"
        gi.write_text("*.pyc\n", encoding="utf-8")
        install_mod.install(self.target, apiary_repo=self.apiary)
        lines = [ln.strip() for ln in gi.read_text(encoding="utf-8").splitlines()]
        # Stepwise, not blanket: a repo must be able to re-include its own
        # slash commands, and git can't un-ignore inside an ignored dir.
        self.assertIn(".claude/*", lines)
        self.assertIn("!.claude/commands/", lines)
        self.assertIn(".claude/commands/*", lines)
        self.assertIn("*.pyc", lines)  # preserved

    def test_gitignore_idempotent_when_already_present(self):
        gi = self.target / ".gitignore"
        gi.write_text(".claude/\n", encoding="utf-8")
        install_mod.install(self.target, apiary_repo=self.apiary)
        text = gi.read_text(encoding="utf-8")
        # No duplicate entry
        self.assertEqual(text.count(".claude/"), 1)

    def test_idempotent_reinstall_keeps_uid_and_registered_at(self):
        first = install_mod.install(self.target, apiary_repo=self.apiary)
        # Second install
        second = install_mod.install(self.target, apiary_repo=self.apiary)
        self.assertEqual(first.uid, second.uid)
        self.assertEqual(first.name, second.name)
        self.assertFalse(second.is_first_install)
        # registered_at should be stable
        registry = json.loads(state.registry_path(self.apiary).read_text(encoding="utf-8"))
        entry = registry[str(first.uid)]
        self.assertEqual(entry["registered_at"], registry[str(second.uid)]["registered_at"])

    def test_registry_entry_has_uid_version_and_name(self):
        result = install_mod.install(self.target, apiary_repo=self.apiary)
        registry = json.loads(state.registry_path(self.apiary).read_text(encoding="utf-8"))
        entry = registry[str(result.uid)]
        self.assertEqual(entry["uid"], result.uid)
        self.assertEqual(entry["version"], "0.1.0")
        self.assertEqual(entry["name"], "demo")
        self.assertEqual(Path(entry["real_path"]), self.target.resolve())

    def test_bootstrap_state_schema_v2_written(self):
        result = install_mod.install(self.target, apiary_repo=self.apiary)
        bs = json.loads(
            (result.state_dir / "bootstrap_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(bs["schema_version"], 2)
        self.assertEqual(bs["profile"], "base")
        self.assertEqual(bs["apiary_version"], "0.1.0")
        self.assertIn("settings_json_hash", bs)
        self.assertIn("commands_dir_hashes", bs)

    def test_install_rejects_non_git_target(self):
        plain = self.root / "no_git"
        plain.mkdir()
        with self.assertRaises(install_mod.InstallError) as ctx:
            install_mod.install(plain, apiary_repo=self.apiary)
        self.assertIn("not inside a git repository", str(ctx.exception))

    def test_install_rejects_nonexistent_target(self):
        ghost = self.root / "does_not_exist"
        with self.assertRaises(install_mod.InstallError):
            install_mod.install(ghost, apiary_repo=self.apiary)

    def test_self_pointer_real_path_matches_target(self):
        install_mod.install(self.target, apiary_repo=self.apiary)
        sp = state.read_self_pointer(self.target)
        self.assertEqual(Path(sp["real_path"]).resolve(), self.target.resolve())

    def test_main_apiary_pointer_path_is_fake_apiary(self):
        install_mod.install(self.target, apiary_repo=self.apiary)
        mp = state.read_main_apiary_pointer(self.target)
        self.assertEqual(Path(mp["main_apiary_path"]).resolve(), self.apiary.resolve())
        self.assertEqual(mp["main_apiary_uid"], 1)


class ScribeTemplateScaffoldTests(unittest.TestCase):
    """Deep review §5a-B: bootstrap seeds <state-dir>/scribe/templates/."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_fake_apiary(self.root)
        self.target = _git_init(self.root / "demo")

    def _templates_dir(self, result) -> Path:
        return result.state_dir / "scribe" / "templates"

    def test_install_scaffolds_a_template_per_type(self):
        from scribe.notes import VALID_TYPES

        result = install_mod.install(self.target, apiary_repo=self.apiary)
        tpl_dir = self._templates_dir(result)
        for note_type in VALID_TYPES:
            self.assertTrue((tpl_dir / f"{note_type}.md").is_file(), f"missing {note_type}.md")

    def test_reinstall_never_overwrites_an_edited_template(self):
        result = install_mod.install(self.target, apiary_repo=self.apiary)
        handoff = self._templates_dir(result) / "handoff.md"
        handoff.write_text("MY OWN TEMPLATE\n", encoding="utf-8")
        install_mod.install(self.target, apiary_repo=self.apiary)
        self.assertEqual(handoff.read_text(encoding="utf-8"), "MY OWN TEMPLATE\n")

    def test_deleted_template_is_not_recreated_mid_session_but_is_on_reinstall(self):
        result = install_mod.install(self.target, apiary_repo=self.apiary)
        todo = self._templates_dir(result) / "todo.md"
        todo.unlink()
        install_mod.install(self.target, apiary_repo=self.apiary)
        self.assertTrue(todo.is_file())

    def test_self_bootstrap_scaffolds_too(self):
        from core import self_bootstrap as sb
        from scribe.notes import VALID_TYPES

        # _make_fake_apiary copies everything install needs; self-bootstrap
        # additionally checks for a migrations/ sentinel.
        (self.apiary / "migrations").mkdir(exist_ok=True)
        _git_init(self.apiary)
        result = sb.self_bootstrap(self.apiary)
        tpl_dir = self._templates_dir(result)
        for note_type in VALID_TYPES:
            self.assertTrue((tpl_dir / f"{note_type}.md").is_file(), f"missing {note_type}.md")


class GeneratedLauncherTests(unittest.TestCase):
    """Execute the generated ``.claude/apiary/launch.py``.

    Every other test only asserts the file exists, so the launcher — the
    single process every hook in every bootstrapped repo goes through — has
    never actually been run by the suite.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_fake_apiary(self.root)
        self.target = _git_init(self.root / "demo")
        install_mod.install(self.target, apiary_repo=self.apiary)
        self.launcher = self.target / ".claude" / "apiary" / "launch.py"

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.launcher), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )

    def test_print_repo_path_reports_main_apiary(self):
        result = self._run("--print-repo-path")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.apiary.resolve())

    def test_dispatch_sets_main_repo_and_state_dir_env(self):
        probe = self.apiary / "probe_env.py"
        probe.write_text(
            "import json, os, sys\n"
            "print(json.dumps({k: os.environ.get(k)\n"
            "                  for k in ('APIARY_MAIN_REPO', 'APIARY_TARGET_STATE_DIR')}))\n",
            encoding="utf-8",
        )
        result = self._run("probe_env.py")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        env = json.loads(result.stdout)
        self.assertEqual(Path(env["APIARY_MAIN_REPO"]), self.apiary.resolve())
        self.assertEqual(
            Path(env["APIARY_TARGET_STATE_DIR"]),
            state.repos_dir(self.apiary) / "demo-1",
        )

    def test_a_removed_hook_script_degrades_quietly(self):
        # Existing repos carry settings.json entries for hooks apiary has
        # since deleted. Until they are re-bootstrapped the launcher is what
        # Claude Code runs, so it must exit 0 with one actionable line and no
        # traceback — anything else surfaces as a hook error every tool call.
        result = self._run("core/hooks/deleted_in_a_later_release.py")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # `{}` is the documented "no opinion" response; an empty stdout with
        # exit 0 is shown as a hook-error notice on every tool call.
        self.assertEqual(result.stdout.strip(), "{}")
        self.assertIn("hook script removed", result.stderr)
        self.assertIn("re-run apiary install", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def test_unreachable_main_apiary_never_blocks(self):
        state.write_main_apiary_pointer(self.target, {
            "main_apiary_path": str(self.root / "gone"),
            "main_apiary_uid": 1,
            "registered_at": "2026-08-26T00:00:00Z",
        })
        result = self._run("core/hooks/inject_session.py")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("running as vanilla Claude session", result.stderr)


if __name__ == "__main__":
    unittest.main()


_FOREIGN_HOOK = "#!/bin/sh\necho mine\n"


class SecretScanHookOnInstallTests(unittest.TestCase):
    """#T-2026-261 — bootstrapping is when the hook must arrive.

    It used to be installed only by the incubator and a standalone script, so
    any repo added through the ordinary `apiary install` path silently had no
    commit-time scan. That decay was observed live: a repo registered half an
    hour after the retrofit sweep had no hook at all.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_fake_apiary(self.root)
        self.target = self.root / "target"
        self.target.mkdir()
        _git_init(self.target)

    def _write_foreign_hook(self):
        hook = self.target / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(_FOREIGN_HOOK, encoding="utf-8")
        return hook

    def test_install_leaves_a_working_hook(self):
        install_mod.install(self.target, apiary_repo=self.apiary)
        hook = git_hooks.hook_path(self.target)
        self.assertTrue(hook.is_file(), "bootstrap should install the pre-commit hook")
        self.assertEqual(git_hooks.classify(hook), "ours")

    def test_reinstall_is_idempotent(self):
        install_mod.install(self.target, apiary_repo=self.apiary)
        install_mod.install(self.target, apiary_repo=self.apiary)
        self.assertEqual(git_hooks.classify(git_hooks.hook_path(self.target)), "ours")

    def test_a_foreign_hook_is_never_clobbered(self):
        hook = self._write_foreign_hook()
        install_mod.install(self.target, apiary_repo=self.apiary)
        self.assertIn("echo mine", hook.read_text(encoding="utf-8"))

    def test_a_refused_hook_does_not_fail_the_install(self):
        # A repo without the hook is still a usable repo; the install must not
        # abort over it, only say so.
        self._write_foreign_hook()
        result = install_mod.install(self.target, apiary_repo=self.apiary)
        self.assertIsNotNone(result)
        self.assertTrue((self.target / ".claude").is_dir())


class GitignoreDotClaudeTests(unittest.TestCase):
    """#T-2026-258 — the exclusion must leave room for repo-owned commands.

    The ticket blamed incubator/templates/gitignore.tmpl, but that file has
    never contained a .claude entry; core/install.py writes it, so this
    affects every bootstrapped repo rather than only incubated ones.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def _gitignore(self):
        return (self.repo / ".gitignore").read_text(encoding="utf-8")

    def test_creates_stepwise_block_when_no_gitignore(self):
        install_mod._ensure_gitignore_entry(self.repo)
        lines = [ln.strip() for ln in self._gitignore().splitlines()]
        self.assertIn(".claude/*", lines)
        self.assertIn("!.claude/commands/", lines)

    def test_is_idempotent_on_the_stepwise_block(self):
        install_mod._ensure_gitignore_entry(self.repo)
        first = self._gitignore()
        install_mod._ensure_gitignore_entry(self.repo)
        self.assertEqual(self._gitignore(), first, "second run must not duplicate")

    def test_leaves_a_pre_existing_blanket_entry_untouched(self):
        # A repo bootstrapped before this change keeps its file byte-for-byte;
        # rewriting somebody's .gitignore unasked is worse than the limitation.
        original = "*.pyc\n.claude/\n"
        (self.repo / ".gitignore").write_text(original, encoding="utf-8")
        install_mod._ensure_gitignore_entry(self.repo)
        self.assertEqual(self._gitignore(), original)


class _InstalledRepoCase(unittest.TestCase):
    """A fake main-apiary plus one already-installed git target."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_fake_apiary(self.root)
        self.target = _git_init(self.root / "demo")
        self.first = install_mod.install(self.target, apiary_repo=self.apiary)
        self.settings_path = self.target / ".claude" / "settings.json"

    def settings(self) -> dict:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def write_settings(self, data: dict) -> None:
        self.settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def reinstall(self):
        return install_mod.install(self.target, apiary_repo=self.apiary)


class ProfileMergeTests(_InstalledRepoCase):
    """Bug 7 — a re-install must not clobber user-owned settings.json keys.

    ``_APIARY_OWNED_KEYS`` is the contract: those keys are regenerated from
    scratch on every install, everything else in the file is the user's and
    only gains the profile's entries.
    """

    PROFILE_PERMISSION = "Bash(*python* *scribe/notes.py *)"

    def test_only_hooks_are_apiary_owned(self):
        # If this set grows, the merge below stops protecting that key —
        # so the constant is asserted rather than assumed.
        self.assertEqual(install_mod._APIARY_OWNED_KEYS, ("hooks",))

    def test_reinstall_keeps_a_user_added_permission(self):
        s = self.settings()
        s["permissions"]["allow"].append("Bash(make *)")
        self.write_settings(s)
        self.reinstall()
        self.assertIn("Bash(make *)", self.settings()["permissions"]["allow"])

    def test_reinstall_keeps_a_user_owned_top_level_key(self):
        s = self.settings()
        s["env"] = {"MY_VAR": "1"}
        s["model"] = "opus"
        self.write_settings(s)
        self.reinstall()
        after = self.settings()
        self.assertEqual(after["env"], {"MY_VAR": "1"})
        self.assertEqual(after["model"], "opus")

    def test_profile_permissions_still_land_on_a_reinstall(self):
        s = self.settings()
        s["permissions"]["allow"] = ["Bash(make *)"]  # user replaced the list
        self.write_settings(s)
        self.reinstall()
        allow = self.settings()["permissions"]["allow"]
        self.assertIn(self.PROFILE_PERMISSION, allow)
        self.assertIn("Bash(make *)", allow)

    def test_a_permission_the_profile_dropped_is_pruned(self):
        # The profile is the source of truth for what apiary contributes, so an
        # entry it stops shipping must go — without taking the user's with it.
        s = self.settings()
        s["permissions"]["allow"].append("Bash(make *)")
        self.write_settings(s)
        (self.apiary / "profiles" / "base.jsonc").write_text(
            json.dumps({"$schema_version": 1, "permissions": {"allow": []}}),
            encoding="utf-8",
        )
        self.reinstall()
        allow = self.settings()["permissions"]["allow"]
        self.assertNotIn(self.PROFILE_PERMISSION, allow)
        self.assertIn("Bash(make *)", allow)

    def test_a_user_hook_entry_survives_a_reinstall(self):
        # Bug 8 end to end: the user's hook names a directory apiary also
        # ships, which used to be enough to get it deleted.
        s = self.settings()
        s["hooks"].setdefault("PreToolUse", []).insert(
            0, {"matcher": "Bash",
                "hooks": [{"type": "command", "command": "python scripts/runner/lint.py"}]},
        )
        self.write_settings(s)
        self.reinstall()
        self.assertIn("scripts/runner/lint.py", json.dumps(self.settings()["hooks"]))

    def test_reinstall_does_not_duplicate_apiary_hook_entries(self):
        before = json.dumps(self.settings()["hooks"])
        self.reinstall()
        self.assertEqual(json.dumps(self.settings()["hooks"]), before)


class SelfPointerReconciliationTests(_InstalledRepoCase):
    """Bug 4 — the self-pointer's uid must never disagree with the registry.

    When it does, the launcher derives ``<name>-<pinned-uid>`` for a state dir
    the registry knows nothing about, so scribe/compass state silently reroutes
    to a fallback path and the drift hook queues messages for an unknown uid.
    """

    def _registry(self) -> dict:
        return json.loads(state.registry_path(self.apiary).read_text(encoding="utf-8"))

    def _second_repo(self, name: str) -> Path:
        repo = self.root / name
        _git_init(repo)
        return repo

    def _lose_the_registry(self) -> None:
        # .repos/ is gitignored, so a fresh clone of main-apiary starts with no
        # registry and no counter, while every bootstrapped repo still carries
        # its pin. A half-failed uninstall (Bug 6) produced the same shape.
        state.registry_path(self.apiary).unlink()
        state.next_id_path(self.apiary).unlink()

    def test_registry_loss_re_adopts_the_pinned_uid(self):
        late = self._second_repo("late")
        first = install_mod.install(late, apiary_repo=self.apiary)
        self.assertEqual(first.uid, 2, "fixture assumption: `demo` took uid 1")
        self._lose_the_registry()
        second = install_mod.install(late, apiary_repo=self.apiary)
        # A fresh counter would have said 1, stranding .repos/late-2 (the
        # repo's whole scribe/compass history) as an orphan.
        self.assertEqual(second.uid, 2)
        self.assertEqual(second.state_dir, first.state_dir)
        self.assertEqual(state.read_self_pointer(late)["uid"], 2)
        self.assertIn("2", self._registry())

    def test_a_re_adopted_uid_is_never_handed_out_again(self):
        late = self._second_repo("late")
        install_mod.install(late, apiary_repo=self.apiary)
        self._lose_the_registry()
        install_mod.install(late, apiary_repo=self.apiary)  # re-adopts uid 2
        newcomer = self._second_repo("newcomer")
        result = install_mod.install(newcomer, apiary_repo=self.apiary)
        self.assertGreater(result.uid, 2)

    def test_a_pin_uid_owned_by_another_repo_is_rewritten(self):
        other = self.root / "other"
        other.mkdir()
        state.write_self_pointer(self.target, {
            "uid": 7, "name": "demo", "real_path": str(self.target),
            "registered_at": "2026-01-01T00:00:00Z",
        })
        state.registry_path(self.apiary).write_text(json.dumps({
            "7": {"name": "other", "real_path": str(other), "uid": 7, "version": "0.1.0"},
        }), encoding="utf-8")
        second = self.reinstall()
        self.assertNotEqual(second.uid, 7)
        self.assertEqual(state.read_self_pointer(self.target)["uid"], second.uid)
        self.assertEqual(self._registry()[str(second.uid)]["real_path"],
                         str(self.target.resolve()))

    def test_last_drift_check_is_preserved(self):
        sp = state.read_self_pointer(self.target)
        sp["last_drift_check"] = "2020-01-01T00:00:00Z"
        state.write_self_pointer(self.target, sp)
        self.reinstall()
        self.assertEqual(state.read_self_pointer(self.target)["last_drift_check"],
                         "2020-01-01T00:00:00Z")


class InstallErrorWrappingTests(_InstalledRepoCase):
    """Bug 10 — `apiary install` must never end in a raw traceback."""

    def test_a_tampered_claude_md_zone_raises_install_error(self):
        (self.target / "CLAUDE.md").write_text(
            "<!-- apiary-context-rules-start -->\n"
            "<!-- apiary-context-rules-start -->\n"
            "<!-- apiary-context-rules-end -->\n",
            encoding="utf-8",
        )
        with self.assertRaises(install_mod.InstallError) as ctx:
            self.reinstall()
        self.assertIn("CLAUDE.md", str(ctx.exception))

    def test_an_unreadable_bootstrap_state_raises_install_error(self):
        (self.first.state_dir / "bootstrap_state.json").write_text(
            "{not json", encoding="utf-8")
        with self.assertRaises(install_mod.InstallError) as ctx:
            self.reinstall()
        self.assertIn("bootstrap_state.json", str(ctx.exception))


@unittest.skipIf(shutil.which("git") is None, "git not on PATH")
class GitignoreSemanticsTests(unittest.TestCase):
    """Ask git itself whether the block does what it claims.

    The stepwise widening is easy to get subtly wrong by reading, so these
    assert against `git check-ignore` rather than against the text.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=str(self.repo), check=False)
        install_mod._ensure_gitignore_entry(self.repo)
        (self.repo / ".claude" / "commands").mkdir(parents=True)

    def _ignored(self, rel: str) -> bool:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=str(self.repo), capture_output=True, check=False,
        )
        return proc.returncode == 0

    def test_apiary_machinery_is_ignored(self):
        (self.repo / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        self.assertTrue(self._ignored(".claude/settings.json"))

    def test_commands_are_ignored_by_default(self):
        # Apiary installs its own commands here; they stay untracked unless
        # the repo owner opts a specific file back in.
        (self.repo / ".claude" / "commands" / "apiary-note.md").write_text("x", encoding="utf-8")
        self.assertTrue(self._ignored(".claude/commands/apiary-note.md"))

    def test_a_named_command_can_be_re_included(self):
        # The whole point of the stepwise form. Under a blanket `.claude/`
        # this re-include is inert, because git will not descend into an
        # ignored directory to reconsider a file.
        gi = self.repo / ".gitignore"
        gi.write_text(
            gi.read_text(encoding="utf-8") + "!.claude/commands/mine.md\n",
            encoding="utf-8",
        )
        (self.repo / ".claude" / "commands" / "mine.md").write_text("x", encoding="utf-8")
        self.assertFalse(self._ignored(".claude/commands/mine.md"))

    def test_blanket_form_cannot_re_include(self):
        # Pins the reason the stepwise form exists, so nobody "simplifies" it
        # back to one line.
        gi = self.repo / ".gitignore"
        gi.write_text(".claude/\n!.claude/commands/mine.md\n", encoding="utf-8")
        (self.repo / ".claude" / "commands" / "mine.md").write_text("x", encoding="utf-8")
        self.assertTrue(
            self._ignored(".claude/commands/mine.md"),
            "a blanket .claude/ should defeat the re-include — if this fails, "
            "git's semantics changed and the stepwise block may be unnecessary",
        )

