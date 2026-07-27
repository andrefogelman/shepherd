"""diffcollect: tree comparison without shepherd-ai."""

from __future__ import annotations

import os
import sys
import tempfile
import time
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

    def test_untouched_files_are_dismissed_without_being_read(self):
        """The stat pair (size, mtime) settles an untouched file, so the diff
        stops re-reading the whole tree on every attempt."""
        base = Path(tempfile.mkdtemp())
        mod = Path(tempfile.mkdtemp())
        for i in range(3):
            (mod / f"f{i}.py").write_text(f"F = {i}\n")
        snap = snapshot_tree(mod)
        time.sleep(0.02)  # clear the snapshot instant (racily-clean guard)
        (mod / "f1.py").write_text("F = 99\n")

        reads: list[str] = []
        real_read = Path.read_bytes

        def counting(self):
            reads.append(self.name)
            return real_read(self)

        Path.read_bytes = counting
        try:
            entries = collect_changed_entries(base, mod, baseline=snap)
        finally:
            Path.read_bytes = real_read

        self.assertEqual(list(entries), ["f1.py"])
        self.assertEqual(reads, ["f1.py"], "only the touched file may be read")

    def test_a_file_stamped_inside_the_snapshot_window_is_still_hashed(self):
        """Filesystem timestamp granularity means a write during the snapshot
        can land on the recorded mtime. Those must not be trusted on stat."""
        base = Path(tempfile.mkdtemp())
        mod = Path(tempfile.mkdtemp())
        target = mod / "racy.py"
        target.write_text("V = 1\n")
        snap = snapshot_tree(mod)
        # same size, and an mtime backdated INTO the snapshot instant
        target.write_text("V = 2\n")
        os.utime(target, ns=(snap.taken_ns, snap.taken_ns))
        entries = collect_changed_entries(base, mod, baseline=snap)
        self.assertEqual(entries, {"racy.py": b"V = 2\n"})

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
