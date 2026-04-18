"""Tests for repo registry and scribe aggregator."""

from __future__ import annotations

import json
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
                (type_dir / f"{seq}.md").write_text(f"body of {rec.get('display_id')}", encoding="utf-8")


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
                [{"display_id": "T-2026-1", "seq": 1, "status": "active", "summary": "a", "timestamp": "2026-04-18T19:00:00Z", "has_body": False}],
                folder="todos",
            )
            _seed_scribe(
                r1,
                [{"display_id": "D-2026-1", "seq": 1, "status": "active", "summary": "decision", "timestamp": "2026-04-18T20:00:00Z", "has_body": False}],
                folder="decisions",
            )
            _seed_scribe(
                r2,
                [{"display_id": "C-2026-1", "seq": 1, "status": "active", "summary": "context", "timestamp": "2026-04-18T18:00:00Z", "has_body": False}],
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
                        json.dumps({"display_id": "T-2026-1", "seq": 1, "status": "active", "summary": "ok", "timestamp": "z", "has_body": False}),
                        "{ partial",
                    ]
                ),
                encoding="utf-8",
            )
            notes, warnings = aggregate([repo])
            self.assertEqual([n.display_id for n in notes], ["T-2026-1"])
            self.assertEqual(warnings, [])

    def test_body_path_resolves_when_file_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            _seed_scribe(
                repo,
                [{"display_id": "T-2026-1", "seq": 5, "status": "active", "summary": "x", "timestamp": "z", "has_body": True}],
            )
            notes, _ = aggregate([repo])
            self.assertEqual(len(notes), 1)
            self.assertTrue(notes[0].body_path)
            self.assertEqual(read_body(notes[0].body_path), "body of T-2026-1")


class RepoRegistryTests(unittest.TestCase):
    def test_load_creates_file_when_missing_and_seeds_with_directories_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "apiary_repos.json"
            self.assertFalse(cfg.exists())
            repos, err = repo_registry.load(cfg)
            self.assertIsNone(err)
            self.assertTrue(cfg.is_file())
            data = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertIsInstance(data, list)
            for entry in data:
                self.assertTrue(Path(entry).is_dir(), f"seed entry not a directory: {entry}")

    def test_load_returns_only_existing_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "apiary_repos.json"
            real = Path(tmp) / "real-repo"
            real.mkdir()
            cfg.write_text(json.dumps([str(real), "C:/this/does/not/exist"]), encoding="utf-8")
            repos, err = repo_registry.load(cfg)
            self.assertIsNone(err)
            self.assertEqual(len(repos), 1)
            self.assertEqual(repos[0].resolve(), real.resolve())

    def test_load_falls_back_on_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "apiary_repos.json"
            cfg.write_text("{not json", encoding="utf-8")
            _repos, err = repo_registry.load(cfg)
            self.assertIsNotNone(err)
            assert err is not None
            self.assertIn("invalid", err)

    def test_load_falls_back_on_non_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "apiary_repos.json"
            cfg.write_text(json.dumps({"repos": []}), encoding="utf-8")
            _repos, err = repo_registry.load(cfg)
            self.assertIsNotNone(err)
            assert err is not None
            self.assertIn("array", err)


if __name__ == "__main__":
    unittest.main()
