"""Tests for the adversarial review panel (K independent reviewers instead
of 1). Runnable with: python -m unittest tests.test_review_panel
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.supervisor import ReviewVerdict, _aggregate_review_verdicts  # noqa: E402


class AggregateReviewVerdictsTests(unittest.TestCase):
    def test_unanimous_approval_is_approved(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="a", issues=[], resolved=[]),
            ReviewVerdict(approved=True, summary="b", issues=[], resolved=[]),
        ])
        self.assertTrue(v.approved)

    def test_a_single_rejection_blocks_the_whole_panel(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="a", issues=[], resolved=[]),
            ReviewVerdict(approved=False, summary="b", issues=["found a bug"], resolved=[]),
        ])
        self.assertFalse(v.approved)

    def test_issues_are_unioned_across_reviewers_deduped(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=False, summary="a", issues=["issue A", "issue B"], resolved=[]),
            ReviewVerdict(approved=False, summary="b", issues=["issue B", "issue C"], resolved=[]),
        ])
        self.assertEqual(v.issues, ["issue A", "issue B", "issue C"])

    def test_resolved_ids_are_unioned_across_reviewers_deduped(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="a", issues=[], resolved=["id1", "id2"]),
            ReviewVerdict(approved=True, summary="b", issues=[], resolved=["id2", "id3"]),
        ])
        self.assertEqual(v.resolved, ["id1", "id2", "id3"])

    def test_any_reviewer_error_makes_the_whole_panel_an_error(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="a", issues=[], resolved=[]),
            ReviewVerdict(approved=False, summary="", error="review run failed: boom"),
        ])
        self.assertFalse(v.approved)
        self.assertIsNotNone(v.error)
        self.assertIn("boom", v.error)

    def test_empty_panel_is_an_error_not_a_silent_approval(self):
        v = _aggregate_review_verdicts([])
        self.assertFalse(v.approved)
        self.assertIsNotNone(v.error)

    def test_summary_credits_every_reviewer(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="looks fine", issues=[], resolved=[]),
            ReviewVerdict(approved=True, summary="also fine", issues=[], resolved=[]),
        ])
        self.assertIn("looks fine", v.summary)
        self.assertIn("also fine", v.summary)


if __name__ == "__main__":
    unittest.main()
