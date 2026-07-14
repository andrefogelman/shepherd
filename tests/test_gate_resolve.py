"""Tests for native-gate test resolution: #7 broadened test-file detection and
#6 the Elixir anti-vacuity sentinel. Runnable with:
    python -m unittest tests.test_gate_resolve
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.supervisor import _proposal_has_elixir_test, _resolve_gate_cmd  # noqa: E402


class TestFileDetection(unittest.TestCase):
    def test_pytest_and_spec_and_go_recognized(self):
        entries = {
            "test_parser.py": b"def test_x(): assert True\n",   # pytest/unittest idiom (#7)
            "app.spec.ts": b"describe('x', () => {})\n",         # .spec.ts (#7)
            "core_test.go": b"package x\n",                       # _test.go (#7)
            "app.py": b"x = 1\n",                                 # not a test
        }
        cmd = _resolve_gate_cmd("python3 -m unittest {NEW_TESTS}", entries)
        assert cmd is not None
        self.assertIn("test_parser.py", cmd)
        self.assertIn("app.spec.ts", cmd)
        self.assertIn("core_test.go", cmd)
        self.assertNotIn("app.py", cmd)

    def test_no_tests_is_none(self):
        self.assertIsNone(_resolve_gate_cmd("python3 -m unittest {NEW_TESTS}", {"app.py": b"x = 1\n"}))

    def test_underscore_test_py_still_matches(self):
        cmd = _resolve_gate_cmd("node --test {NEW_TESTS}", {"a.test.mjs": b"", "b.py": b""})
        assert cmd is not None
        self.assertIn("a.test.mjs", cmd)


class ElixirAntiVacuity(unittest.TestCase):
    def test_exunit_sentinel_requires_a_test(self):
        # no ExUnit test shipped -> gate resolves to None (would fail loudly)
        self.assertIsNone(_resolve_gate_cmd("mix test {EXUNIT_TESTS}", {"lib/thing.ex": b"defmodule T do\nend\n"}))

    def test_exunit_sentinel_stripped_when_test_present(self):
        entries = {"test/thing_test.exs": b"defmodule TTest do\n  use ExUnit.Case\n  test \"x\" do\n  end\nend\n"}
        self.assertEqual(_resolve_gate_cmd("mix test {EXUNIT_TESTS}", entries), "mix test")

    def test_has_elixir_test(self):
        self.assertTrue(_proposal_has_elixir_test({"test/a_test.exs": b"use ExUnit.Case\n"}))
        self.assertFalse(_proposal_has_elixir_test({"lib/a.ex": b"defmodule A do\nend\n"}))


if __name__ == "__main__":
    unittest.main()
