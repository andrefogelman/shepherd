"""Tests for the no-progress guard (phase 4 of the review-rounds work).

Feedback the worker does not act on produces the same proposal again. Spending
the rest of the budget re-judging a byte-identical changeset buys nothing, and
it hides the real problem behind "attempts exhausted" — which reads like the
task was hard rather than like the loop was stuck.
Runnable with: python -m unittest tests.test_no_progress
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.progress import format_event  # noqa: E402

try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


class NoProgressRenderTests(unittest.TestCase):
    def test_event_renders_with_its_reason(self):
        s = format_event({"kind": "attempt.no_progress", "payload": {"attempt": 2}})
        self.assertIsNotNone(s)
        assert s is not None
        self.assertIn("identical", s.lower())


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class NoProgressLoopTests(unittest.TestCase):
    def _run(self, *, proposals, gate_passes=None, verdicts=None, review_rounds=1,
             max_attempts=3):
        """proposals: the changeset each worker attempt produces, in order."""
        from shepherd_dev import supervisor as sup

        calls = {"worker": 0, "review": 0, "gates": 0, "discards": 0}

        class _Output:
            def __init__(self, n):
                self.n = n

            def changeset(self):
                return dict(proposals[min(self.n, len(proposals)) - 1])

            def discard(self):
                calls["discards"] += 1

        class _Run:
            def __init__(self, n):
                self.run_ref = f"run-{n}"
                self._out = _Output(n)

            def output(self):
                return self._out

        class _Tasks:
            def register(self, task):
                pass

        class _Workspace:
            tasks = _Tasks()

            def run(self, task, **kw):
                calls["worker"] += 1
                return _Run(calls["worker"])

        def _gate(repo_root, entries, test_cmd, timeout, **kw):
            i = calls["gates"]
            calls["gates"] += 1
            passed = False if gate_passes is None else gate_passes[i]
            return sup.GateResult(passed, 0 if passed else 1, "gate output")

        def _review(workspace, review_task, **kw):
            i = calls["review"]
            calls["review"] += 1
            approved, issues = (verdicts or [(True, [])])[i]
            return sup.ReviewVerdict(approved=approved, summary="s", issues=list(issues))

        orig = (sup.read_changeset_entries, sup._run_gate, sup.run_review,
                sup._start_gate_warmup)
        sup.read_changeset_entries = dict
        sup._run_gate = _gate
        sup.run_review = _review
        sup._start_gate_warmup = lambda *a, **k: None
        try:
            report = sup.develop(
                _Workspace(), object(), repo=object(), repo_root=Path("/r"),
                feature="add X", test_cmd="pytest -q",
                review_task=object() if verdicts else None,
                max_attempts=max_attempts, review_rounds=review_rounds,
            )
        finally:
            (sup.read_changeset_entries, sup._run_gate, sup.run_review,
             sup._start_gate_warmup) = orig
        return report, calls

    def test_identical_changeset_stops_the_loop(self):
        same = {"a.py": b"A = 1\n"}
        report, calls = self._run(proposals=[same, same, same], max_attempts=3)
        self.assertEqual(calls["worker"], 2)  # never spends the third
        self.assertEqual(report.outcome, "blocked")
        assert report.blocked_reason is not None
        self.assertIn("no progress", report.blocked_reason)

    def test_the_repeated_proposal_is_not_left_retained(self):
        same = {"a.py": b"A = 1\n"}
        report, calls = self._run(proposals=[same, same], max_attempts=3)
        self.assertEqual(calls["discards"], 2)
        self.assertIsNone(report.final_run_ref)

    def test_first_attempt_can_never_be_no_progress(self):
        report, calls = self._run(proposals=[{"a.py": b"A = 1\n"}], max_attempts=1)
        self.assertEqual(calls["worker"], 1)
        self.assertEqual(report.outcome, "failed")
        self.assertIsNone(report.blocked_reason)

    def test_different_content_keeps_going(self):
        report, calls = self._run(
            proposals=[{"a.py": b"A = 1\n"}, {"a.py": b"A = 2\n"}, {"a.py": b"A = 3\n"}],
            max_attempts=3,
        )
        self.assertEqual(calls["worker"], 3)
        self.assertEqual(report.outcome, "failed")

    def test_same_content_under_a_different_path_keeps_going(self):
        report, calls = self._run(
            proposals=[{"a.py": b"A = 1\n"}, {"b.py": b"A = 1\n"}], max_attempts=2
        )
        self.assertEqual(calls["worker"], 2)
        self.assertIsNone(report.blocked_reason)

    def test_comparison_ignores_the_order_files_come_back_in(self):
        report, _ = self._run(
            proposals=[
                {"a.py": b"A\n", "b.py": b"B\n"},
                {"b.py": b"B\n", "a.py": b"A\n"},
            ],
            max_attempts=3,
        )
        self.assertEqual(report.outcome, "blocked")

    def test_it_also_catches_a_stuck_rework_round(self):
        # The proposal passes the gate, the reviewer rejects it, and the rework
        # round hands back exactly the same files.
        same = {"a.py": b"A = 1\n"}
        report, calls = self._run(
            proposals=[same, same], gate_passes=[True, True],
            verdicts=[(False, ["issue A"]), (False, ["issue A"])],
            review_rounds=3, max_attempts=2,
        )
        self.assertEqual(calls["worker"], 2)
        self.assertEqual(calls["review"], 1)  # the repeat never reaches review
        self.assertEqual(report.outcome, "blocked")
        assert report.blocked_reason is not None
        self.assertIn("no progress", report.blocked_reason)


if __name__ == "__main__":
    unittest.main()
