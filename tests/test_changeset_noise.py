"""Build residue a worker's own tool runs leave behind is not a proposal.

Observed on a real run: the worker, now told which command judges it, ran
the suite; `__pycache__/*.pyc` entered the changeset as two of four "changed
files", and their binary content reached the reviewer's prompt and killed
its launch with `embedded null byte`.

Runnable with: python -m unittest tests.test_changeset_noise
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.supervisor import (  # noqa: E402
    _is_changeset_noise,
    _one_file_text,
    read_changeset_entries,
)


class Noise(unittest.TestCase):
    def test_bytecode_and_cache_dirs_are_noise_at_any_depth(self):
        for rel in ("__pycache__/calc.cpython-314.pyc", "tests/__pycache__/t.pyc", "pkg/.pytest_cache/v/x",
                    "a.pyc", "node_modules/x/index.js", ".venv/bin/python", ".mypy_cache/3.12/x.json"):
            with self.subTest(rel=rel):
                self.assertTrue(_is_changeset_noise(rel))

    def test_source_is_not(self):
        for rel in ("calc.py", "tests/test_calc.py", "src/pycache_helper.py", "docs/__pycache__.md"):
            with self.subTest(rel=rel):
                self.assertFalse(_is_changeset_noise(rel))

    def test_read_changeset_entries_drops_it_before_reading(self):
        reads: list[str] = []

        class _CS:
            changed_paths = ["__pycache__/calc.cpython-314.pyc", "calc.py", "tests/__pycache__/t.pyc", "tests/test_calc.py"]

            def read_file(self, rel):
                reads.append(rel)
                return (b"content", 0o100644)

        entries = read_changeset_entries(_CS())
        self.assertEqual(sorted(entries), ["calc.py", "tests/test_calc.py"])
        self.assertEqual(sorted(reads), ["calc.py", "tests/test_calc.py"])


class BinaryInTheDiff(unittest.TestCase):
    def test_a_binary_file_is_named_not_rendered(self):
        text = _one_file_text("blob.bin", b"\x00\x01\x02abc", None)
        self.assertEqual(text, "=== FILE: blob.bin (binary, 6 bytes; not shown) ===\n")
        self.assertNotIn("\x00", text)

    def test_text_still_renders(self):
        self.assertIn("hello", _one_file_text("a.txt", b"hello\n", None))


if __name__ == "__main__":
    unittest.main()
