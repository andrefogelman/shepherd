"""Tests for the explicit run outcome (phase 1 of the review-rounds work).

`succeeded` means "the gate passed", not "this is done" — an orchestrator that
routes on it alone ships a proposal the reviewer rejected. `outcome` names the
five states `_auto_settle_conditions` already distinguishes, so prose and JSON
both say plainly what happened.
Runnable with: python -m unittest tests.test_outcome
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


def _report(succeeded: bool = True, review: str | None = "approved", blocked: str | None = None):
    from shepherd_dev.supervisor import Attempt, DevReport, GateResult, ReviewVerdict

    report = DevReport(feature="add X", succeeded=succeeded, repo="/r")
    report.final_run_ref = "run-abc" if succeeded else None
    report.attempts = [
        Attempt(1, "run-abc", ["a.py"], [], GateResult(True, 0, "ok"), "passed", duration_s=3.2)
    ]
    report.entries = {"a.py": b"A = 1\n"}
    if review == "approved":
        report.review = ReviewVerdict(approved=True, summary="fine", issues=[])
    elif review == "rejected":
        report.review = ReviewVerdict(approved=False, summary="two problems", issues=["i1", "i2"])
    elif review == "error":
        report.review = ReviewVerdict(approved=False, summary="", error="verdict unreadable")
    if blocked:
        report.blocked_reason = blocked
    return report


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class OutcomeDerivationTests(unittest.TestCase):
    def test_failed_when_gate_never_passed(self):
        self.assertEqual(_report(succeeded=False, review=None).outcome, "failed")

    def test_passed_approved(self):
        self.assertEqual(_report().outcome, "passed_approved")

    def test_passed_rejected(self):
        self.assertEqual(_report(review="rejected").outcome, "passed_rejected")

    def test_no_review_is_not_approval(self):
        # --no-review / --provider static: the gate passed and NOBODY approved.
        self.assertEqual(_report(review=None).outcome, "passed_unreviewed")

    def test_unavailable_verdict_is_not_approval(self):
        self.assertEqual(_report(review="error").outcome, "passed_unreviewed")

    def test_blocked_wins_over_a_passing_gate(self):
        r = _report(blocked="no progress: identical changeset twice")
        self.assertEqual(r.outcome, "blocked")

    def test_blocked_wins_over_failure_too(self):
        r = _report(succeeded=False, review=None, blocked="ledger has no actionable item")
        self.assertEqual(r.outcome, "blocked")

    def test_default_report_has_no_blocked_reason(self):
        from shepherd_dev.supervisor import DevReport

        self.assertIsNone(DevReport(feature="f", succeeded=False).blocked_reason)


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class OutcomeMatchesAutoSettleTests(unittest.TestCase):
    """The invariant that makes `outcome` trustworthy: it is exactly the
    question auto-settle already asks, so the two can never drift apart."""

    def test_only_passed_approved_may_auto_settle(self):
        from shepherd_dev.cli import _auto_settle_conditions

        cases = [
            _report(),
            _report(review="rejected"),
            _report(review=None),
            _report(review="error"),
            _report(succeeded=False, review=None),
            _report(blocked="stuck"),
        ]
        for r in cases:
            with self.subTest(outcome=r.outcome):
                refused = _auto_settle_conditions(r)
                self.assertEqual(refused is None, r.outcome == "passed_approved")


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class OutcomeSurfacedTests(unittest.TestCase):
    def test_summary_states_the_outcome(self):
        text = _report(review="rejected").summary()
        self.assertIn("outcome: passed_rejected", text)
        self.assertIn("review: REJECTED", text)

    def test_summary_states_the_blocked_reason(self):
        text = _report(blocked="no progress: identical changeset twice").summary()
        self.assertIn("outcome: blocked", text)
        self.assertIn("no progress: identical changeset twice", text)

    def test_payload_carries_the_outcome(self):
        from shepherd_dev import history

        payload = history.run_payload(
            _report(review="rejected"), Path("/r"),
            mode="feature", test_cmd="pytest -q", provider="claude", flags={},
        )
        self.assertEqual(payload["outcome"], "passed_rejected")
        self.assertTrue(payload["succeeded"])  # kept for compatibility

    def test_envelope_carries_the_outcome(self):
        from shepherd_dev.cli import _report_envelope

        env = _report_envelope(
            _report(review="rejected"), repo_root=Path("/r"), mode="feature",
            test_cmd="pytest -q", provider="claude", verbose_run="rid-1",
        )
        self.assertEqual(env["outcome"], "passed_rejected")
        json.dumps(env)

    def test_envelope_blocked_reason_is_machine_readable(self):
        from shepherd_dev.cli import _report_envelope

        env = _report_envelope(
            _report(blocked="stuck on backend change"), repo_root=Path("/r"),
            mode="feature", test_cmd="pytest -q", provider="claude",
        )
        self.assertEqual(env["outcome"], "blocked")
        self.assertEqual(env["blocked_reason"], "stuck on backend change")


if __name__ == "__main__":
    unittest.main()
