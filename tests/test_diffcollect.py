"""diffcollect: tree comparison without shepherd-ai."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.diffcollect import (  # noqa: E402
    collect_changed_entries,
    snapshot_tree,
)


class CollectChanged(unittest.TestCase):
    def test_new_and_modified(self):
        base = Path(tempfile.mkdtemp())
        mod = Path(tempfile.mkdtemp())
        (base / "a.py").write_text("a=1\n")
        (base / "b.py").write_text("b=1\n")
        (mod / "a.py").write_text("a=1\n")  # unchanged
        (mod / "b.py").write_text("b=2\n")  # modified
        (mod / "c.py").write_text("c=3\n")  # new
        entries = collect_changed_entries(base, mod)
        self.assertNotIn("a.py", entries)
        self.assertEqual(entries["b.py"], b"b=2\n")
        self.assertEqual(entries["c.py"], b"c=3\n")

    def test_ignores_venv(self):
        base = Path(tempfile.mkdtemp())
        mod = Path(tempfile.mkdtemp())
        (mod / ".venv").mkdir()
        (mod / ".venv" / "x.py").write_text("nope\n")
        (mod / "ok.py").write_text("ok\n")
        entries = collect_changed_entries(base, mod)
        self.assertEqual(list(entries), ["ok.py"])


class BaselineSnapshot(unittest.TestCase):
    """#3: the base a proposal is diffed against must be the tree as it was
    when the worker started — NOT the live repo, which the user may edit
    mid-run."""

    def test_concurrent_edit_to_original_does_not_enter_the_proposal(self):
        base = Path(tempfile.mkdtemp())
        mod = Path(tempfile.mkdtemp())
        (base / "untouched.py").write_text("v=1\n")
        (base / "target.py").write_text("t=1\n")
        (mod / "untouched.py").write_text("v=1\n")  # worker never touched it
        (mod / "target.py").write_text("t=1\n")
        snap = snapshot_tree(mod)

        # worker does its work...
        (mod / "target.py").write_text("t=2\n")
        # ...while the human edits an unrelated file in the real repo
        (base / "untouched.py").write_text("v=2  # human edit\n")

        entries = collect_changed_entries(base, mod, baseline=snap)
        self.assertNotIn("untouched.py", entries)  # would silently revert it
        self.assertEqual(entries["target.py"], b"t=2\n")

    def test_without_baseline_the_stale_file_leaks_in(self):
        # Pins the reason the baseline exists: the live-tree comparison is
        # exactly what drags an unrelated human edit into the changeset.
        base = Path(tempfile.mkdtemp())
        mod = Path(tempfile.mkdtemp())
        (base / "untouched.py").write_text("v=1\n")
        (mod / "untouched.py").write_text("v=1\n")
        (base / "untouched.py").write_text("v=2  # human edit\n")
        self.assertIn("untouched.py", collect_changed_entries(base, mod))

    def test_snapshot_covers_new_files_and_ignores_noise(self):
        mod = Path(tempfile.mkdtemp())
        (mod / "a.py").write_text("a=1\n")
        (mod / ".venv").mkdir()
        (mod / ".venv" / "x.py").write_text("nope\n")
        snap = snapshot_tree(mod)
        self.assertEqual(list(snap), ["a.py"])
        (mod / "b.py").write_text("b=1\n")  # created by the worker
        entries = collect_changed_entries(mod, mod, baseline=snap)
        self.assertEqual(list(entries), ["b.py"])


if __name__ == "__main__":
    unittest.main()
