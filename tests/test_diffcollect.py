"""diffcollect: tree comparison without shepherd-ai."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.diffcollect import collect_changed_entries  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
