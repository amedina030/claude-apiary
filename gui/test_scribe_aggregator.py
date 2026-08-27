"""Tests for repo registry and scribe aggregator."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from gui import repo_registry
from gui.scribe_aggregator import aggregate, read_body


def _seed_scribe(repo: Path, notes: list[dict], folder: str = "todos", year: str = "2026") -> None:
    type_dir = repo / ".apiary" / "scribe" / folder / year
    type_dir.mkdir(parents=True, exist_ok=True)
    index = type_dir / "index.jsonl"
    with index.open("w", encoding="utf-8") as f:
        for rec in notes:
            f.write(json.dumps(rec) + "\n")
            seq = rec.get("seq")
            if rec.get("has_body") and seq is not None:
                (type_dir / f"{seq}.md").write_text(
                    f"body of {rec.get('display_id')}", encoding="utf-8"
                )


class AggregateTests(unittest.TestCase):
    def test_picks_up_active_notes_and_skips_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repoA"
            repo.mkdir()
            _seed_scribe(
                repo,
                [
                    {
                        "display_id": "T-2026-1",
                        "seq": 1,
                        "status": "active",
                        "summary": "first todo",
                        "timestamp": "2026-04-18T19:00:00Z",
                        "has_body": True,
                    },
                    {
                        "display_id": "T-2026-2",
                        "seq": 2,
                        "status": "done",
                        "summary": "should be filtered",
                        "timestamp": "2026-04-18T19:01:00Z",
                        "has_body": False,
                    },
                    {
                        "display_id": "T-2026-3",
                        "seq": 3,
                        "status": "deferred",
                        "summary": "deferred kept",
                        "timestamp": "2026-04-18T19:02:00Z",
                        "has_body": True,
                    },
                ],
            )
            notes, warnings = aggregate([repo])
            self.assertEqual(warnings, [])
            ids = sorted(n.display_id for n in notes)
            self.assertEqual(ids, ["T-2026-1", "T-2026-3"])
            for n in notes:
                self.assertEqual(n.type, "todo")
                self.assertEqual(n.folder, "todos")
                self.assertEqual(n.repo_label, "repoA")

    def test_aggregates_across_multiple_repos_and_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            r1 = Path(tmp) / "r1"
            r2 = Path(tmp) / "r2"
            r1.mkdir()
            r2.mkdir()
            _seed_scribe(
                r1,
                [
                    {
                        "display_id": "T-2026-1",
                        "seq": 1,
                        "status": "active",
                        "summary": "a",
                        "timestamp": "2026-04-18T19:00:00Z",
                        "has_body": False,
                    }
                ],
                folder="todos",
            )
            _seed_scribe(
                r1,
                [
                    {
                        "display_id": "D-2026-1",
                        "seq": 1,
                        "status": "active",
                        "summary": "decision",
                        "timestamp": "2026-04-18T20:00:00Z",
                        "has_body": False,
                    }
                ],
                folder="decisions",
            )
            _seed_scribe(
                r2,
                [
                    {
                        "display_id": "C-2026-1",
                        "seq": 1,
                        "status": "active",
                        "summary": "context",
                        "timestamp": "2026-04-18T18:00:00Z",
                        "has_body": False,
                    }
                ],
                folder="context",
            )
            notes, warnings = aggregate([r1, r2])
            self.assertEqual(warnings, [])
            self.assertEqual(len(notes), 3)
            # Newest first.
            self.assertEqual([n.display_id for n in notes], ["D-2026-1", "T-2026-1", "C-2026-1"])
            self.assertEqual({n.type for n in notes}, {"todo", "decision", "context"})

    def test_skips_repo_without_apiary(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            notes, warnings = aggregate([empty])
            self.assertEqual(notes, [])
            self.assertEqual(warnings, [])

    def test_malformed_index_line_skipped_others_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            type_dir = repo / ".apiary" / "scribe" / "todos" / "2026"
            type_dir.mkdir(parents=True)
            (type_dir / "index.jsonl").write_text(
                "\n".join(
                    [
                        "not json",
                        json.dumps(
                            {
                                "display_id": "T-2026-1",
                                "seq": 1,
                                "status": "active",
                                "summary": "ok",
                                "timestamp": "z",
                                "has_body": False,
                            }
                        ),
                        "{ partial",
                    ]
                ),
                encoding="utf-8",
            )
            notes, warnings = aggregate([repo])
            self.assertEqual([n.display_id for n in notes], ["T-2026-1"])
            self.assertEqual(warnings, [])

    def test_brief_summary_passed_through_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            _seed_scribe(
                repo,
                [
                    {
                        "display_id": "T-2026-1",
                        "seq": 1,
                        "status": "active",
                        "summary": "Long summary text that would wrap badly.",
                        "brief_summary": "Short headline.",
                        "timestamp": "z",
                        "has_body": True,
                    }
                ],
            )
            notes, _ = aggregate([repo])
            self.assertEqual(notes[0].brief_summary, "Short headline.")
            d = notes[0].to_dict()
            self.assertEqual(d["brief_summary"], "Short headline.")

    def test_brief_summary_absent_defaults_to_empty(self):
        # Pre-migration entries lack the field — aggregator must still load.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            _seed_scribe(
                repo,
                [
                    {
                        "display_id": "T-2026-1",
                        "seq": 1,
                        "status": "active",
                        "summary": "ok",
                        "timestamp": "z",
                        "has_body": False,
                    }
                ],
            )
            notes, _ = aggregate([repo])
            self.assertEqual(notes[0].brief_summary, "")

    def test_body_path_resolves_when_file_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            _seed_scribe(
                repo,
                [
                    {
                        "display_id": "T-2026-1",
                        "seq": 5,
                        "status": "active",
                        "summary": "x",
                        "timestamp": "z",
                        "has_body": True,
                    }
                ],
            )
            notes, _ = aggregate([repo])
            self.assertEqual(len(notes), 1)
            self.assertTrue(notes[0].body_path)
            self.assertEqual(read_body(notes[0].body_path), "body of T-2026-1")


def _make_pointer(target_repo: Path, apiary: Path, target_id: str) -> None:
    pointer_dir = target_repo / ".apiary"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    (pointer_dir / "pointer").write_text(
        json.dumps(
            {
                "apiary_repo": str(apiary.resolve()),
                "target_id": target_id,
                "registered_at": "2026-05-04T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def _seed_state_dir(
    state_dir: Path, notes: list[dict], folder: str = "todos", year: str = "2026"
) -> None:
    """Seed scribe data under <state_dir>/scribe/ (post-migration layout)."""
    type_dir = state_dir / "scribe" / folder / year
    type_dir.mkdir(parents=True, exist_ok=True)
    index = type_dir / "index.jsonl"
    with index.open("w", encoding="utf-8") as f:
        for rec in notes:
            f.write(json.dumps(rec) + "\n")


class AggregatePointerTests(unittest.TestCase):
    """Post-migration: scribe state lives at <apiary>/.repos/<target_id>/scribe/.
    The aggregator must resolve target -> state-dir via the per-target pointer
    file written by core/utils/state.py at registration time.
    """

    def test_pointer_resolves_to_centralized_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            apiary = Path(tmp) / "apiary"
            target = Path(tmp) / "target"
            target_id = "target-7"
            state_dir = apiary / ".repos" / target_id
            state_dir.mkdir(parents=True)
            target.mkdir()
            _make_pointer(target, apiary, target_id)
            _seed_state_dir(
                state_dir,
                [
                    {
                        "display_id": "T-2026-1",
                        "seq": 1,
                        "status": "active",
                        "summary": "centralized",
                        "timestamp": "z",
                        "has_body": False,
                    },
                ],
            )
            notes, warnings = aggregate([target])
            self.assertEqual(warnings, [])
            self.assertEqual([n.display_id for n in notes], ["T-2026-1"])

    def test_pointer_takes_precedence_over_legacy_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            apiary = Path(tmp) / "apiary"
            target = Path(tmp) / "target"
            target_id = "target-7"
            state_dir = apiary / ".repos" / target_id
            state_dir.mkdir(parents=True)
            target.mkdir()
            _make_pointer(target, apiary, target_id)
            _seed_state_dir(
                state_dir,
                [
                    {
                        "display_id": "T-2026-NEW",
                        "seq": 1,
                        "status": "active",
                        "summary": "centralized",
                        "timestamp": "z",
                        "has_body": False,
                    },
                ],
            )
            # Stale legacy data left behind by an incomplete migration —
            # the pointer-resolved state must win so the GUI doesn't show
            # ghost notes.
            _seed_scribe(
                target,
                [
                    {
                        "display_id": "T-2026-OLD",
                        "seq": 1,
                        "status": "active",
                        "summary": "legacy",
                        "timestamp": "z",
                        "has_body": False,
                    },
                ],
            )
            notes, _ = aggregate([target])
            self.assertEqual([n.display_id for n in notes], ["T-2026-NEW"])

    def test_falls_back_to_legacy_when_pointer_state_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            apiary = Path(tmp) / "apiary"
            target = Path(tmp) / "target"
            target.mkdir()
            # Pointer exists but the centralized state directory does not.
            _make_pointer(target, apiary, "target-99")
            _seed_scribe(
                target,
                [
                    {
                        "display_id": "T-2026-LEG",
                        "seq": 1,
                        "status": "active",
                        "summary": "legacy",
                        "timestamp": "z",
                        "has_body": False,
                    },
                ],
            )
            notes, _ = aggregate([target])
            self.assertEqual([n.display_id for n in notes], ["T-2026-LEG"])

    def test_no_pointer_uses_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            _seed_scribe(
                target,
                [
                    {
                        "display_id": "T-2026-1",
                        "seq": 1,
                        "status": "active",
                        "summary": "ok",
                        "timestamp": "z",
                        "has_body": False,
                    },
                ],
            )
            notes, _ = aggregate([target])
            self.assertEqual([n.display_id for n in notes], ["T-2026-1"])


class RepoRegistryTests(unittest.TestCase):
    """Post-2026-05 the GUI's repo registry reads from main-apiary's
    ``.repos/registry.json`` directly — it is the only source."""

    def test_load_returns_real_paths_from_registry(self):
        # Hermetic: point the resolver at a temp registry rather than reading
        # the operator's real one, which is absent in a worktree and varies
        # per machine (T-2026-274).
        import unittest.mock as _mock

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            live = tmp_p / "live-repo"
            live.mkdir()
            reg = tmp_p / "registry.json"
            reg.write_text(
                json.dumps(
                    {
                        "1": {"real_path": str(live)},
                        "2": {"real_path": str(tmp_p / "deleted-repo")},
                        "3": {"real_path": ""},
                        "4": "not-a-dict",
                    }
                ),
                encoding="utf-8",
            )
            with _mock.patch.object(repo_registry, "_registry_path", return_value=reg):
                repos, err = repo_registry.load()
            self.assertIsNone(err, f"unexpected error: {err}")
            # Entries that no longer exist on disk (and malformed rows) are
            # dropped; each returned entry is a real directory.
            self.assertEqual([r.resolve() for r in repos], [live.resolve()])
            for r in repos:
                self.assertTrue(r.is_dir(), f"non-dir: {r}")

    def test_load_returns_error_when_registry_missing(self):
        import unittest.mock as _mock

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            with _mock.patch.object(
                repo_registry, "_registry_path", return_value=tmp_p / "missing-registry.json"
            ):
                repos, err = repo_registry.load()
            self.assertEqual(repos, [])
            self.assertIsNotNone(err)

    def test_registry_path_source_build_is_checkout_root(self):
        # gui/repo_registry.py lives at <checkout>/gui/, so the registry is
        # <checkout>/.repos/registry.json for a source run.
        checkout = Path(repo_registry.__file__).resolve().parent.parent
        self.assertEqual(repo_registry._registry_path(), checkout / ".repos" / "registry.json")

    def test_registry_path_frozen_anchors_to_checkout_not_bundle(self):
        # #T-2026-248: the packaged picker's "Recent" rail was always empty
        # because the registry path was this file's grandparent, which in a
        # PyInstaller bundle is <bundle>/_internal — no registry there.
        import unittest.mock as _mock

        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            (checkout / ".git").mkdir()
            (checkout / "gui").mkdir()
            exe = checkout / "dist" / "apiary-gui" / "apiary-gui.exe"
            exe.parent.mkdir(parents=True)
            exe.touch()
            with (
                _mock.patch.object(sys, "frozen", True, create=True),
                _mock.patch.object(sys, "executable", str(exe)),
            ):
                self.assertEqual(
                    repo_registry._registry_path(),
                    checkout / ".repos" / "registry.json",
                )

    def test_load_reads_registry_from_frozen_checkout(self):
        # End-to-end shape of the bug: a frozen build must surface the repos
        # registered in the checkout it ships from, not an empty list.
        import unittest.mock as _mock

        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            (checkout / ".git").mkdir()
            (checkout / "gui").mkdir()
            exe = checkout / "dist" / "apiary-gui" / "apiary-gui.exe"
            exe.parent.mkdir(parents=True)
            exe.touch()
            registered = checkout / "some-repo"
            registered.mkdir()
            reg = checkout / ".repos" / "registry.json"
            reg.parent.mkdir()
            reg.write_text(json.dumps({"1": {"real_path": str(registered)}}), encoding="utf-8")
            with (
                _mock.patch.object(sys, "frozen", True, create=True),
                _mock.patch.object(sys, "executable", str(exe)),
            ):
                repos, err = repo_registry.load()
            self.assertIsNone(err)
            self.assertEqual([r.resolve() for r in repos], [registered.resolve()])


if __name__ == "__main__":
    unittest.main()
