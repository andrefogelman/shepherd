"""diffcollect: tree comparison without shepherd-ai."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev.diffcollect import (  # noqa: E402
    collect_changed_entries,
    snapshot_tree,
)


class CollectChanged(unittest.TestCase):
    def test_new_and_modified(self):
        base = Path(mkdtemp())
        mod = Path(mkdtemp())
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
        base = Path(mkdtemp())
        mod = Path(mkdtemp())
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
        base = Path(mkdtemp())
        mod = Path(mkdtemp())
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
        base = Path(mkdtemp())
        mod = Path(mkdtemp())
        (base / "untouched.py").write_text("v=1\n")
        (mod / "untouched.py").write_text("v=1\n")
        (base / "untouched.py").write_text("v=2  # human edit\n")
        self.assertIn("untouched.py", collect_changed_entries(base, mod))

    def test_a_rewrite_of_the_same_size_on_the_same_mtime_is_still_detected(self):
        """Comparison is by CONTENT, never by stat metadata.

        Linux stamps mtime from a coarse clock (one jiffy), so a same-size
        rewrite in the tick the snapshot was taken in carries a byte-identical
        (size, mtime) pair. A stat fast-path would call that "untouched" and
        drop the worker's edit from the changeset in silence — the same
        disappearing-work failure #3 exists to prevent. Simulated here with
        utime, since APFS timestamps are too fine-grained to hit it naturally.
        """
        base = Path(mkdtemp())
        mod = Path(mkdtemp())
        target = mod / "target.py"
        target.write_text("t=1\n")
        st = target.stat()
        snap = snapshot_tree(mod)

        target.write_text("t=2\n")                       # same size, new content
        os.utime(target, ns=(st.st_mtime_ns, st.st_mtime_ns))  # same mtime
        self.assertEqual(target.stat().st_size, st.st_size)
        self.assertEqual(target.stat().st_mtime_ns, st.st_mtime_ns)

        self.assertEqual(collect_changed_entries(base, mod, baseline=snap),
                         {"target.py": b"t=2\n"})

    def test_snapshot_covers_new_files_and_ignores_noise(self):
        mod = Path(mkdtemp())
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
