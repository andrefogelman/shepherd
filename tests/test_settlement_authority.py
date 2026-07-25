"""Tests for the settlement invariant of the rework loop (phase 6).

Rework gives the loop a second chance to answer the reviewer. It must not give
it a second way to declare itself done. Settling is the human's act: the loop
retains a proposal and stops, and every state rework can reach — approved on a
later round, rejected out of rounds, blocked on a verdict with no finding, a
round whose gate went red — is judged by the same rule as a first-round run.
The reports here come from the real develop() loop, not hand-built ones, so what
is checked is the state the loop actually leaves behind.
Runnable with: python -m unittest tests.test_settlement_authority
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class ReworkSettlementTests(unittest.TestCase):
    """develop() driven by fakes; the loop and _auto_settle_conditions are real."""

    def _drive(self, *, verdicts, review_rounds=2, max_attempts=2, gate_passes=None):
        from shepherd_dev import supervisor as sup

        calls: dict = {"worker": 0, "review": 0, "gates": 0, "select": 0, "discard": 0}

        class _Output:
            def __init__(self, n):
                self.n = n

            def changeset(self):
                return {"tests/a.py": f"v{self.n}\n".encode()}

            def discard(self):
                calls["discard"] += 1

            def select(self):  # the settlement act, on the real Output too
                calls["select"] += 1

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
            passed = True if gate_passes is None else gate_passes[min(i, len(gate_passes) - 1)]
            return sup.GateResult(passed, 0 if passed else 1, "gate output")

        def _review(workspace, review_task, **kw):
            i = calls["review"]
            calls["review"] += 1
            approved, issues = verdicts[min(i, len(verdicts) - 1)]
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
                feature="add X", test_cmd="pytest -q", review_task=object(),
                max_attempts=max_attempts, review_rounds=review_rounds,
            )
        finally:
            (sup.read_changeset_entries, sup._run_gate, sup.run_review,
             sup._start_gate_warmup) = orig
        return report, calls

    def _refusal(self, report):
        from shepherd_dev.cli import _auto_settle_conditions

        return _auto_settle_conditions(report)

    # -- the loop does not settle ----------------------------------------------
    def test_develop_never_settles_even_after_a_rework_round(self):
        report, calls = self._drive(verdicts=[(False, ["issue A"]), (True, [])])

        self.assertEqual(report.outcome, "passed_approved")
        self.assertEqual(calls["worker"], 2)  # the rework round really ran
        # The approved proposal is retained, not consumed: only the human's
        # settle verb may call select().
        self.assertEqual(calls["select"], 0)
        self.assertEqual(report.final_run_ref, "run-2")

    def test_the_supervisor_holds_no_settlement_machinery(self):
        # Structural, not behavioral: settlement lives in cli.settle_run /
        # settle_proposal. A rework path that grew its own accept would be a way
        # around _auto_settle_conditions no test driving develop() could see.
        # materialize_into is defined here and called on the gate's staging dirs
        # — writing it into repo_root is what would advance the world, so that
        # call shape is the forbidden one, not the helper.
        source = (Path(__file__).resolve().parent.parent
                  / "src" / "shepherd_dev" / "supervisor.py").read_text()
        for forbidden in (".select(", "settle_run", "settle_proposal",
                          "materialize_into(repo_root"):
            self.assertNotIn(forbidden, source, forbidden)

    # -- what each rework-reachable state is allowed to do ---------------------
    def test_a_reworked_approval_is_treated_exactly_like_a_first_round_one(self):
        direct, _ = self._drive(verdicts=[(True, [])], review_rounds=1)
        reworked, calls = self._drive(verdicts=[(False, ["issue A"]), (True, [])])

        self.assertEqual(calls["worker"], 2)  # it really took a second round
        self.assertEqual(reworked.outcome, direct.outcome)
        self.assertIsNone(self._refusal(direct))
        self.assertIsNone(self._refusal(reworked))

    def test_a_rejection_that_ran_out_of_rounds_is_retained_but_refused(self):
        report, calls = self._drive(verdicts=[(False, ["issue A"])], review_rounds=2)

        self.assertEqual(calls["review"], 2)  # both rounds were reviewed
        self.assertEqual(report.outcome, "passed_rejected")
        # Retained for the human: the ref and the changeset are still there…
        self.assertIsNotNone(report.final_run_ref)
        self.assertTrue(report.entries)
        # …and auto-settle still refuses it.
        self.assertEqual(self._refusal(report), "review REJECTED the proposal")
        self.assertEqual(calls["select"], 0)

    def test_a_rework_round_whose_gate_goes_red_leaves_nothing_settleable(self):
        # Round 1 passes and is rejected; round 2 fails the gate. The rejected
        # output was discarded, so nothing settleable may still point at it.
        report, calls = self._drive(
            verdicts=[(False, ["issue A"])], gate_passes=[True, False],
            review_rounds=2, max_attempts=1,
        )
        self.assertEqual(report.outcome, "failed")
        self.assertIsNone(report.final_run_ref)
        self.assertIsNone(report.entries)
        self.assertEqual(self._refusal(report), "run did not succeed")
        self.assertGreaterEqual(calls["discard"], 1)

    def test_a_rejection_with_no_actionable_finding_blocks_instead_of_settling(self):
        # The state rework itself created: gate green, ref retained, verdict
        # rejecting — and nothing to rework, so the loop stops short of done.
        report, calls = self._drive(verdicts=[(False, [])], review_rounds=2)

        self.assertEqual(report.outcome, "blocked")
        self.assertEqual(calls["worker"], 1)  # no round spent on an empty objection
        self.assertIsNotNone(report.final_run_ref)  # still there for the human
        self.assertEqual(
            self._refusal(report),
            "run is blocked: reviewer rejected but left no actionable finding",
        )
        self.assertEqual(calls["select"], 0)

    # -- more rounds buy attempts, not authority -------------------------------
    def test_more_rounds_never_turn_a_rejection_into_an_approval(self):
        one, few = self._drive(verdicts=[(False, ["A"])], review_rounds=1)
        many, lots = self._drive(verdicts=[(False, ["A"])], review_rounds=5)

        self.assertGreater(lots["worker"], few["worker"])  # more rounds were spent
        self.assertEqual(many.outcome, one.outcome)
        self.assertEqual(self._refusal(many), self._refusal(one))
        self.assertIsNotNone(self._refusal(many))

    def test_no_rework_reachable_outcome_may_auto_settle_but_passed_approved(self):
        cases = {
            "approved in round 1": dict(verdicts=[(True, [])], review_rounds=1),
            "approved after rework": dict(verdicts=[(False, ["A"]), (True, [])]),
            "rejected out of rounds": dict(verdicts=[(False, ["A"])]),
            "rejected with no finding": dict(verdicts=[(False, [])]),
            "rework gate went red": dict(verdicts=[(False, ["A"])], gate_passes=[True, False],
                                         max_attempts=1),
        }
        for label, kw in cases.items():
            report, _ = self._drive(**kw)
            with self.subTest(case=label, outcome=report.outcome):
                self.assertEqual(
                    self._refusal(report) is None, report.outcome == "passed_approved"
                )


if __name__ == "__main__":
    unittest.main()
