"""--auto-settle applies a proposal without asking. What may authorise that.

heuristic_review counts files and bytes. It approves anything under 30 files
and 200KB, having read none of it — its own docstring says "a weak advisory
signal — auto-settle still requires a real reviewing provider". Nothing
enforced that: _auto_settle_conditions asked only whether a review existed and
approved, and grok has no reviewer CLI, so `--provider grok --auto-settle`
wrote a bounded-looking diff into the worktree on the strength of its size.

Runnable with: python -m unittest tests.test_auto_settle_gate
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.cli import _auto_settle_conditions  # noqa: E402
from shepherd_dev.providers.hosted import heuristic_review  # noqa: E402
from shepherd_dev.supervisor import ReviewVerdict  # noqa: E402


class _Report:
    """The shape _auto_settle_conditions reads."""

    def __init__(self, review, succeeded=True, blocked_reason=None):
        self.review = review
        self.succeeded = succeeded
        self.blocked_reason = blocked_reason


class HeuristicReviewIsAdvisory(unittest.TestCase):
    def test_it_marks_itself_advisory(self):
        v = heuristic_review({"a.py": b"x = 1\n"}, "f")
        self.assertTrue(v.approved)      # it does approve
        self.assertTrue(v.advisory)      # ...and says the approval is not a review

    def test_an_llm_verdict_is_not_advisory(self):
        self.assertFalse(ReviewVerdict(approved=True, summary="s").advisory)


class AutoSettleGate(unittest.TestCase):
    def test_an_advisory_approval_does_not_authorise_auto_settle(self):
        reason = _auto_settle_conditions(_Report(heuristic_review({"a.py": b"x\n"}, "f")))
        self.assertIsNotNone(reason)
        self.assertIn("advisory", reason or "")

    def test_a_real_approval_still_authorises_it(self):
        self.assertIsNone(
            _auto_settle_conditions(_Report(ReviewVerdict(approved=True, summary="ok")))
        )

    def test_the_existing_gates_are_unchanged(self):
        approved = ReviewVerdict(approved=True, summary="ok")
        for report, expected in (
            (_Report(None), "no review"),
            (_Report(ReviewVerdict(approved=False, summary="no")), "REJECTED"),
            (_Report(ReviewVerdict(approved=True, summary="", error="down")), "unavailable"),
            (_Report(approved, succeeded=False), "did not succeed"),
            (_Report(approved, blocked_reason="out of rounds"), "blocked"),
        ):
            with self.subTest(expected=expected):
                reason = _auto_settle_conditions(report)
                self.assertIsNotNone(reason)
                self.assertIn(expected, reason or "")


if __name__ == "__main__":
    unittest.main()
