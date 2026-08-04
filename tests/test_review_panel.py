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


try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class RunReviewPanelTests(unittest.TestCase):
    """Real clones (via the same _clone_many parallel workers already use),
    real sp.open per clone — only the reviewer's own AI call is faked, same
    boundary LocalGateStageTests/SpeculativeReviewTests already fake at."""

    def setUp(self):
        import subprocess
        import tempfile

        from shepherd_dev import supervisor as S

        self.repo = Path(tempfile.mkdtemp(prefix="shepherd-panel-"))
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("V = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli", "init", "--repo", str(self.repo)],
            check=True, capture_output=True, text=True,
        )
        self._orig_run_review = S.run_review

    def tearDown(self):
        from shepherd_dev import supervisor as S

        S.run_review = self._orig_run_review

    def test_unanimous_approval_from_three_independent_clones(self):
        from shepherd_dev import supervisor as S

        calls = []

        def _fake_review(workspace, review_task, **kw):
            calls.append(kw.get("feature"))
            return S.ReviewVerdict(approved=True, summary="fine", issues=[], resolved=[])

        S.run_review = _fake_review
        verdict = S.run_review_panel(
            self.repo, object(), 3, feature="add X", diff_text="+V = 2\n",
        )
        self.assertTrue(verdict.approved)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(f == "add X" for f in calls))

    def test_one_dissenter_blocks_and_its_issue_survives(self):
        from shepherd_dev import supervisor as S

        n = {"i": 0}

        def _fake_review(workspace, review_task, **kw):
            n["i"] += 1
            if n["i"] == 2:
                return S.ReviewVerdict(approved=False, summary="found it", issues=["real bug"], resolved=[])
            return S.ReviewVerdict(approved=True, summary="fine", issues=[], resolved=[])

        S.run_review = _fake_review
        verdict = S.run_review_panel(self.repo, object(), 3, feature="add X")
        self.assertFalse(verdict.approved)
        self.assertIn("real bug", verdict.issues)

    def test_clones_are_cleaned_up_after_the_panel_runs(self):
        import tempfile as _tempfile

        from shepherd_dev import supervisor as S

        before = set(Path(_tempfile.gettempdir()).glob("shepherd-par-*"))
        S.run_review = lambda workspace, review_task, **kw: S.ReviewVerdict(
            approved=True, summary="x", issues=[], resolved=[]
        )
        S.run_review_panel(self.repo, object(), 2, feature="add X")
        after = set(Path(_tempfile.gettempdir()).glob("shepherd-par-*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
