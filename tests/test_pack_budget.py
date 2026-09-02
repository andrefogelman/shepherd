"""The pack keeps room for files, and the planner's targets go in first.

Measured before: the pack hit its 25k ceiling on 100% of real runs and the
cut fell wherever the scored list ended; 98 of 120 runs had planned targets
and one of them landed by the planned path, because that path ran last.

Runnable with: python -m unittest tests.test_pack_budget
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.contextpack import FILES_RESERVE, _tree, build_pack  # noqa: E402


def _repo(n_files: int, path_len: int = 80) -> Path:
    repo = Path(tempfile.mkdtemp(prefix="shepherd-pack-"))
    deep = repo / ("d" * path_len)
    deep.mkdir()
    for i in range(n_files):
        (deep / f"module_{i:04d}.py").write_text(f"VALUE_{i} = {i}\n" + "# widget\n" * 30)
    (repo / "widget.py").write_text("def widget():\n    return 1\n")
    return repo


class TreeRoom(unittest.TestCase):
    def test_the_tree_shrinks_to_the_room_it_is_given_and_says_how_much_it_dropped(self):
        repo = _repo(200)
        files = sorted(repo.rglob("*.py"))
        full = _tree(repo, files)
        self.assertGreater(len(full), 2_000)
        small = _tree(repo, files, room=2_000)
        self.assertLessEqual(len(small), 2_000)
        self.assertIn("more files)", small)

    def test_no_room_limit_means_the_old_listing(self):
        repo = _repo(5)
        files = sorted(repo.rglob("*.py"))
        self.assertEqual(_tree(repo, files), _tree(repo, files, room=None))


class FilesReserve(unittest.TestCase):
    def test_a_long_tree_cannot_eat_the_file_budget(self):
        repo = _repo(300, path_len=120)
        pack, stats = build_pack(repo, "widget value")
        self.assertLessEqual(stats["chars"], 25_000)
        tree = pack.split("== REPO FILE TREE ==\n", 1)[1].split("\n== FILE:", 1)[0]
        self.assertLess(len(tree), 25_000 - FILES_RESERVE)
        # and files did land
        self.assertGreater(stats["files_full"] + stats["files_skeleton"], 0)


class PlannedFirst(unittest.TestCase):
    def test_the_planners_target_lands_even_when_scored_files_would_fill_the_pack(self):
        repo = _repo(300)
        # a target that keyword scoring would never pick (no keyword hit)
        (repo / "obscure_target.py").write_text("def obscure():\n    return 42\n")
        pack, stats = build_pack(
            repo, "widget widget widget",
            planned_targets=("obscure_target.py",), plan_text="1. edit obscure_target.py",
        )
        self.assertIn("== FILE: obscure_target.py (planned target; full) ==", pack)
        self.assertEqual(stats["planned"], 1)
        self.assertIn("== FEATURE PLAN", pack)
        # the planned block precedes every keyword-scored block
        self.assertLess(pack.index("planned target; full"), pack.index("== FILE: widget.py (full) =="))

    def test_a_planned_target_already_scored_is_not_counted_twice(self):
        repo = _repo(3)
        pack, stats = build_pack(repo, "widget", planned_targets=("widget.py",))
        self.assertEqual(pack.count("== FILE: widget.py"), 1)
        self.assertEqual(stats["planned"], 1)


if __name__ == "__main__":
    unittest.main()
