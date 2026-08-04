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


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class DevelopReviewPanelWiringTests(unittest.TestCase):
    """develop() driven by fakes — same harness style as
    test_review_rounds.py's DevelopReworkLoopTests._run, extended with a
    review_panel arg and a fake for run_review_panel specifically (so these
    tests check ROUTING, not the panel's own clone/aggregate mechanics —
    those are Task 1/2's job)."""

    def _run(self, *, review_panel, panel_verdict=None, gate_passes=None):
        from shepherd_dev import supervisor as sup

        calls = {"worker": 0, "review": 0, "panel": 0, "panel_size": None, "gates": []}

        class _Output:
            def changeset(self):
                return {"file.py": b"v1\n"}

            def discard(self):
                pass

        class _Run:
            run_ref = "run-1"

            def output(self):
                return _Output()

        class _Tasks:
            def register(self, task):
                pass

        class _Workspace:
            tasks = _Tasks()

            def run(self, task, **kw):
                calls["worker"] += 1
                return _Run()

        def _read_entries(changeset):
            return dict(changeset)

        def _gate(repo_root, entries, test_cmd, timeout, **kw):
            i = len(calls["gates"])
            passed = True if gate_passes is None else gate_passes[i]
            calls["gates"].append(passed)
            return sup.GateResult(passed, 0 if passed else 1, "gate output")

        def _review(workspace, review_task, **kw):
            calls["review"] += 1
            return sup.ReviewVerdict(approved=True, summary="s", issues=[], resolved=[])

        def _review_panel(repo_root, review_task, size, **kw):
            calls["panel"] += 1
            calls["panel_size"] = size
            return panel_verdict or sup.ReviewVerdict(approved=True, summary="p", issues=[], resolved=[])

        orig = (
            sup.read_changeset_entries, sup._run_gate, sup.run_review,
            sup.run_review_panel, sup._start_gate_warmup,
        )
        sup.read_changeset_entries = _read_entries
        sup._run_gate = _gate
        sup.run_review = _review
        sup.run_review_panel = _review_panel
        sup._start_gate_warmup = lambda *a, **k: None
        try:
            report = sup.develop(
                _Workspace(), object(), repo=object(), repo_root=Path("/r"),
                feature="add X", test_cmd="pytest -q", review_task=object(),
                max_attempts=1, review_panel=review_panel,
            )
        finally:
            (
                sup.read_changeset_entries, sup._run_gate, sup.run_review,
                sup.run_review_panel, sup._start_gate_warmup,
            ) = orig
        return report, calls

    def test_panel_size_one_calls_run_review_not_the_panel(self):
        _, calls = self._run(review_panel=1)
        self.assertEqual(calls["review"], 1)
        self.assertEqual(calls["panel"], 0)

    def test_panel_size_above_one_calls_the_panel_not_run_review(self):
        _, calls = self._run(review_panel=3)
        self.assertEqual(calls["review"], 0)
        self.assertEqual(calls["panel"], 1)
        self.assertEqual(calls["panel_size"], 3)

    def test_the_panels_verdict_is_the_reports_verdict(self):
        from shepherd_dev import supervisor as sup

        v = sup.ReviewVerdict(approved=False, summary="p", issues=["x"], resolved=[])
        report, _ = self._run(review_panel=2, panel_verdict=v)
        self.assertIs(report.review, v)

    def test_default_review_panel_is_one(self):
        import inspect

        from shepherd_dev import supervisor as sup

        self.assertEqual(inspect.signature(sup.develop).parameters["review_panel"].default, 1)


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class ReviewPanelCliTests(unittest.TestCase):
    def test_flag_defaults_to_none_not_one(self):
        # None, not 1: this is how _resolve_review_panel tells "not passed"
        # apart from "explicitly passed 1" — same trick --test-cmd already
        # uses (cli.py's p_run.add_argument("--test-cmd", default=None, ...)).
        from shepherd_dev.cli import build_parser

        args = build_parser().parse_args(["run", "add X"])
        self.assertIsNone(args.review_panel)

    def test_flag_is_accepted_up_to_the_cap(self):
        from shepherd_dev.cli import MAX_REVIEW_PANEL, build_parser

        self.assertEqual(MAX_REVIEW_PANEL, 5)
        args = build_parser().parse_args(["run", "add X", "--review-panel", "5"])
        self.assertEqual(args.review_panel, 5)

    def test_above_the_cap_is_refused(self):
        from shepherd_dev.cli import _validate_review_panel

        self.assertIsNotNone(_validate_review_panel(6, no_review=False, provider="claude"))
        self.assertIsNone(_validate_review_panel(5, no_review=False, provider="claude"))

    def test_below_one_is_refused(self):
        from shepherd_dev.cli import _validate_review_panel

        self.assertIsNotNone(_validate_review_panel(0, no_review=False, provider="claude"))

    def test_panel_without_a_reviewer_is_refused(self):
        from shepherd_dev.cli import _validate_review_panel

        self.assertIsNotNone(_validate_review_panel(2, no_review=True, provider="claude"))
        self.assertIsNotNone(_validate_review_panel(2, no_review=False, provider="static"))
        # one reviewer is the status quo — must stay legal everywhere
        self.assertIsNone(_validate_review_panel(1, no_review=True, provider="static"))

    def test_resolve_prefers_explicit_over_saved_over_default(self):
        import tempfile

        from shepherd_dev import config
        from shepherd_dev.cli import _resolve_review_panel

        repo = Path(tempfile.mkdtemp(prefix="shepherd-panel-resolve-"))
        self.assertEqual(_resolve_review_panel(repo, None), 1)  # no config, no flag
        config.save_config(repo, {"review_panel": 3})
        self.assertEqual(_resolve_review_panel(repo, None), 3)  # saved config wins over default
        self.assertEqual(_resolve_review_panel(repo, 2), 2)  # explicit flag wins over saved config


if __name__ == "__main__":
    unittest.main()
