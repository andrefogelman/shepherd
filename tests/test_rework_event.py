"""Tests for the review.rework event (phase 7 of the review-rounds work).

An extra round spent on the reviewer's objections has to be visible in the
trace, or the run looks like it silently took longer.
Runnable with: python -m unittest tests.test_rework_event
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.progress import format_event  # noqa: E402


def _ev(kind, payload):
    return {"kind": kind, "payload": payload}


class ReworkEventRenderTests(unittest.TestCase):
    def test_rework_names_the_round_and_the_open_count(self):
        s = format_event(_ev("review.rework", {"round": 2, "of": 3, "open": 4}))
        self.assertIsNotNone(s)
        assert s is not None
        self.assertIn("2/3", s)
        self.assertIn("4", s)

    def test_rework_renders_in_live_mode_too(self):
        # It marks a real restart of the cycle — hiding it live would make the
        # spinner sit on "review" while a whole new worker attempt runs.
        s = format_event(_ev("review.rework", {"round": 2, "of": 2, "open": 1}), live=True)
        self.assertIsNotNone(s)

    def test_singular_open_finding_reads_correctly(self):
        s = format_event(_ev("review.rework", {"round": 2, "of": 5, "open": 1}))
        assert s is not None
        self.assertIn("1 open finding", s)

    def test_plural_open_findings_reads_correctly(self):
        s = format_event(_ev("review.rework", {"round": 3, "of": 5, "open": 3}))
        assert s is not None
        self.assertIn("3 open findings", s)

    def test_rework_stop_states_the_reason(self):
        s = format_event(_ev("review.rework.stop", {"reason": "no progress: identical changeset"}))
        assert s is not None
        self.assertIn("no progress", s)


if __name__ == "__main__":
    unittest.main()
