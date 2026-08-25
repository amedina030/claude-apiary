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

from core import install as install_mod
from core.utils import state


# Items the install needs to find under main-apiary. Tests copy the real
# files into a tmpdir "fake apiary" so each test gets an isolated install
# target without touching the real registry under ``D:\Professional\claude-apiary\.repos``.
_APIARY_ITEMS = (
    "setup.py", "VERSION", "core", "profiles", "context-rules",
    "budgeter", "scribe", "docs", "refiner", "harden",
    "compass", "researcher", "runner", "incubator",
)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=path, check=True,
    )


def _make_fake_apiary(root: Path) -> Path:
    """Copy enough of the real apiary into *root*/apiary_copy that install
    can run against it. Returns the fake apiary path."""
    fake = root / "apiary_copy"
    fake.mkdir()
    for item in _APIARY_ITEMS:
        src = REPO_ROOT / item
        if src.is_dir():
            shutil.copytree(src, fake / item, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, fake / item)
    (fake / ".repos").mkdir()
    (fake / ".apiary" / "forwarding").mkdir(parents=True)
    return fake


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_fake_apiary(self.root)
        self.target = self.root / "demo"
        self.target.mkdir()
        _git_init(self.target)

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


if __name__ == "__main__":
    unittest.main()


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

