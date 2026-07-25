"""Tests for --review-rounds (phase 3 of the review-rounds work).

Gate PASSED plus reviewer REJECTED used to end the cycle: the proposal stayed
retained and the objections went nowhere. A rework round feeds the open ledger
back to the worker and spends another pass, on its own budget — a gate failure
must never eat the rework allowance, and rework must never eat the attempts
reserved for failures.
Runnable with: python -m unittest tests.test_review_rounds
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.ledger import Ledger, finding_id  # noqa: E402
from shepherd_dev.prompts import get_prompt  # noqa: E402

try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


class ReworkGuidanceTests(unittest.TestCase):
    def test_prompt_key_is_registered_and_optimizable(self):
        from shepherd_dev.optimize import EDITABLE_KEYS
        from shepherd_dev.prompts import DEFAULT_PROMPTS, PROMPT_KEYS

        self.assertIn("guidance_review", PROMPT_KEYS)
        self.assertIn("guidance_review", DEFAULT_PROMPTS)
        self.assertIn("guidance_review", EDITABLE_KEYS)

    def test_template_carries_the_findings_token(self):
        self.assertIn("{FINDINGS}", get_prompt("guidance_review"))

    def test_guidance_lists_open_findings_with_their_ids(self):
        from shepherd_dev.supervisor import _format_guidance

        led = Ledger()
        led.record_round(1, ["cache is never invalidated", "toast hides the error"])
        led.close(finding_id("toast hides the error"), "refused", reason="human accepts")
        text = _format_guidance("review", ledger=led)
        self.assertIn("cache is never invalidated", text)
        self.assertIn(finding_id("cache is never invalidated"), text)
        self.assertNotIn("toast hides the error", text)  # closed items are not re-litigated

    def test_guidance_forbids_relabelling_instead_of_fixing(self):
        from shepherd_dev.supervisor import _format_guidance

        led = Ledger()
        led.record_round(1, ["cache is never invalidated"])
        text = _format_guidance("review", ledger=led).lower()
        self.assertTrue(
            "severity" in text or "re-label" in text or "relabel" in text,
            "the rework prompt must say that downgrading a finding is not fixing it",
        )


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class ReviewRoundsCliTests(unittest.TestCase):
    def test_flag_defaults_to_one_round(self):
        from shepherd_dev.cli import build_parser

        args = build_parser().parse_args(["run", "add X"])
        self.assertEqual(args.review_rounds, 1)

    def test_flag_is_accepted_up_to_the_cap(self):
        from shepherd_dev.cli import MAX_REVIEW_ROUNDS, build_parser

        self.assertEqual(MAX_REVIEW_ROUNDS, 5)
        args = build_parser().parse_args(["run", "add X", "--review-rounds", "5"])
        self.assertEqual(args.review_rounds, 5)

    def test_above_the_cap_is_refused(self):
        from shepherd_dev.cli import _validate_review_rounds

        self.assertIsNotNone(_validate_review_rounds(6, no_review=False, provider="claude"))
        self.assertIsNone(_validate_review_rounds(5, no_review=False, provider="claude"))

    def test_below_one_is_refused(self):
        from shepherd_dev.cli import _validate_review_rounds

        self.assertIsNotNone(_validate_review_rounds(0, no_review=False, provider="claude"))

    def test_rework_with_best_of_is_refused_rather_than_ignored(self):
        from shepherd_dev.cli import _validate_review_rounds

        self.assertIsNotNone(
            _validate_review_rounds(2, no_review=False, provider="claude", best_of=3)
        )
        self.assertIsNone(
            _validate_review_rounds(1, no_review=False, provider="claude", best_of=3)
        )

    def test_rework_without_a_reviewer_is_refused(self):
        from shepherd_dev.cli import _validate_review_rounds

        self.assertIsNotNone(_validate_review_rounds(2, no_review=True, provider="claude"))
        self.assertIsNotNone(_validate_review_rounds(2, no_review=False, provider="static"))
        # one round is the status quo — it must stay legal everywhere
        self.assertIsNone(_validate_review_rounds(1, no_review=True, provider="static"))


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class DevelopReworkLoopTests(unittest.TestCase):
    """develop() driven by fakes: the substrate is stubbed, the loop is real."""

    def _run(self, *, verdicts, review_rounds=1, max_attempts=3, gate_passes=None,
             review_error=False, on_review=None, reporter=None):
        """verdicts: (approved, issues[, resolved]) per review call. gate_passes:
        per attempt. on_review: called with each review call's kwargs."""
        from shepherd_dev import supervisor as sup

        calls = {"worker": 0, "review": 0, "gates": [], "discards": 0, "guidance": []}

        class _Output:
            def __init__(self, n):
                self.n = n

            def changeset(self):
                return {"file.py": f"v{self.n}\n".encode()}

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
                calls["guidance"].append(kw.get("guidance", ""))
                return _Run(calls["worker"])

        def _read_entries(changeset):
            return dict(changeset)

        def _gate(repo_root, entries, test_cmd, timeout, **kw):
            i = len(calls["gates"])
            passed = True if gate_passes is None else gate_passes[i]
            calls["gates"].append(passed)
            return sup.GateResult(passed, 0 if passed else 1, "gate output")

        def _review(workspace, review_task, **kw):
            i = calls["review"]
            calls["review"] += 1
            if on_review is not None:
                on_review(kw)
            if review_error:
                return sup.ReviewVerdict(approved=False, summary="", error="unreadable")
            approved, issues, *rest = verdicts[i]
            return sup.ReviewVerdict(
                approved=approved,
                summary="s",
                issues=list(issues),
                resolved=list(rest[0]) if rest else [],
            )

        orig = (sup.read_changeset_entries, sup._run_gate, sup.run_review, sup._start_gate_warmup)
        sup.read_changeset_entries = _read_entries
        sup._run_gate = _gate
        sup.run_review = _review
        sup._start_gate_warmup = lambda *a, **k: None
        try:
            report = sup.develop(
                _Workspace(), object(), repo=object(), repo_root=Path("/r"),
                feature="add X", test_cmd="pytest -q", review_task=object(),
                max_attempts=max_attempts, review_rounds=review_rounds,
                **({"reporter": reporter} if reporter is not None else {}),
            )
        finally:
            (sup.read_changeset_entries, sup._run_gate, sup.run_review,
             sup._start_gate_warmup) = orig
        return report, calls

    def test_one_round_keeps_todays_behaviour_exactly(self):
        report, calls = self._run(verdicts=[(False, ["issue A"])], review_rounds=1)
        self.assertEqual(calls["worker"], 1)
        self.assertEqual(calls["review"], 1)
        self.assertTrue(report.succeeded)
        self.assertEqual(report.outcome, "passed_rejected")
        self.assertEqual(report.final_run_ref, "run-1")  # retained for the human

    def test_rejection_spends_a_rework_round(self):
        report, calls = self._run(
            verdicts=[(False, ["issue A"]), (True, [])], review_rounds=2
        )
        self.assertEqual(calls["worker"], 2)
        self.assertEqual(calls["review"], 2)
        self.assertEqual(report.outcome, "passed_approved")
        self.assertEqual(report.final_run_ref, "run-2")

    def test_the_rejected_proposal_is_discarded_before_reworking(self):
        _, calls = self._run(verdicts=[(False, ["issue A"]), (True, [])], review_rounds=2)
        self.assertEqual(calls["discards"], 1)

    def test_the_rework_round_is_told_what_is_open(self):
        _, calls = self._run(verdicts=[(False, ["cache never invalidated"]), (True, [])],
                             review_rounds=2)
        self.assertIn("cache never invalidated", calls["guidance"][1])

    def test_rounds_are_capped_and_the_last_proposal_is_retained(self):
        report, calls = self._run(
            verdicts=[(False, ["issue A"]), (False, ["issue A"])], review_rounds=2
        )
        self.assertEqual(calls["worker"], 2)
        self.assertEqual(report.outcome, "passed_rejected")
        self.assertEqual(report.final_run_ref, "run-2")  # nothing thrown away at the end

    def test_the_ledger_records_every_round(self):
        # Round 2 stops mentioning issue B without saying it is gone. That is
        # not evidence of a fix — most often it is a reword — so B stays open.
        report, _ = self._run(
            verdicts=[(False, ["issue A", "issue B"]), (False, ["issue A"])], review_rounds=2
        )
        assert report.ledger is not None
        by_id = {f.id: f for f in report.ledger.findings}
        self.assertEqual(by_id[finding_id("issue B")].state, "open")
        self.assertEqual(by_id[finding_id("issue A")].state, "open")
        self.assertEqual(by_id[finding_id("issue A")].rounds, [1, 2])

    def test_a_finding_the_reviewer_reports_resolved_closes(self):
        # The whole wiring end to end: REVIEW.json's `resolved` reaches the
        # ledger and is the thing that closes an item short of approval.
        report, _ = self._run(
            verdicts=[
                (False, ["issue A", "issue B"]),
                (False, ["issue A"], [finding_id("issue B")]),
            ],
            review_rounds=2,
        )
        assert report.ledger is not None
        by_id = {f.id: f for f in report.ledger.findings}
        self.assertEqual(by_id[finding_id("issue B")].state, "fixed")
        self.assertEqual(by_id[finding_id("issue A")].state, "open")

    def test_the_second_round_reviewer_is_handed_the_open_findings(self):
        seen: list[str] = []
        report, _ = self._run(
            verdicts=[(False, ["issue A"]), (False, ["issue A"])],
            review_rounds=2,
            on_review=lambda kw: seen.append(kw.get("findings", "")),
        )
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], "")  # round 1 has no prior findings
        self.assertIn(finding_id("issue A"), seen[1])
        self.assertIn("issue A", seen[1])

    def test_approval_never_spends_a_rework_round(self):
        report, calls = self._run(verdicts=[(True, [])], review_rounds=5)
        self.assertEqual(calls["worker"], 1)
        self.assertEqual(report.outcome, "passed_approved")

    def test_gate_failures_do_not_consume_the_rework_allowance(self):
        # attempt 1 fails the gate, attempt 2 passes but is rejected, the
        # rework round then passes and is approved.
        report, calls = self._run(
            verdicts=[(False, ["issue A"]), (True, [])],
            review_rounds=2, max_attempts=2, gate_passes=[False, True, True],
        )
        self.assertEqual(calls["worker"], 3)
        self.assertEqual(report.outcome, "passed_approved")

    def test_rework_does_not_consume_the_attempt_allowance(self):
        # max_attempts=1 leaves no room for failures, but a rework round is a
        # different budget and must still be spent.
        report, calls = self._run(
            verdicts=[(False, ["issue A"]), (True, [])], review_rounds=2, max_attempts=1
        )
        self.assertEqual(calls["worker"], 2)
        self.assertEqual(report.outcome, "passed_approved")

    def test_an_unavailable_verdict_never_triggers_rework(self):
        report, calls = self._run(verdicts=[(False, [])], review_rounds=3, review_error=True)
        self.assertEqual(calls["worker"], 1)  # nothing to rework against
        self.assertEqual(report.outcome, "passed_unreviewed")
        assert report.ledger is not None
        self.assertEqual(report.ledger.findings, [])  # a broken verdict records nothing

    def test_a_rejection_with_no_issues_blocks_instead_of_looping(self):
        # REJECTED with an empty issue list gives the worker nothing to act on;
        # burning rounds against it would be pure waste.
        report, calls = self._run(verdicts=[(False, [])], review_rounds=3)
        self.assertEqual(calls["worker"], 1)
        self.assertEqual(report.outcome, "blocked")
        assert report.blocked_reason is not None
        self.assertIn("no actionable", report.blocked_reason)


class TheReviewerSeesTheOpenFindings(unittest.TestCase):
    """Round 2's reviewer used to start blank, so it re-derived round 1's
    objections and inevitably reworded them — which read as one finding fixed
    and one newly found. It is handed the open list and answers about it."""

    def test_the_prompt_teaches_the_id_protocol(self):
        text = get_prompt("review")
        self.assertIn("findings", text)
        self.assertIn("resolved", text)
        self.assertIn("square brackets", text)

    def test_the_review_task_accepts_the_findings_argument(self):
        import inspect

        from shepherd_dev.tasks import review

        target = getattr(review, "__wrapped__", None) or getattr(review, "fn", None) or review
        try:
            params = inspect.signature(target).parameters
        except (TypeError, ValueError):
            self.skipTest("task object exposes no inspectable signature")
        self.assertIn("findings", params, "the reviewer cannot be handed the open list")

    def _drive_review(self, review_json: bytes, findings: str):
        """run_review against a stub workspace, returning (args, verdict)."""
        import shepherd_dev.supervisor as sup

        captured: dict = {}

        class _Output:
            def changeset(self):
                return {"REVIEW.json": review_json}

            def discard(self):
                pass

        class _Run:
            def output(self):
                return _Output()

        class _Workspace:
            class tasks:
                @staticmethod
                def register(task):
                    pass

            def git_repo(self):
                return object()

            def run(self, task, **kw):
                captured.update(kw)
                return _Run()

        orig = sup.read_changeset_entries
        sup.read_changeset_entries = lambda cs: dict(cs)
        try:
            verdict = sup.run_review(
                _Workspace(), object(), feature="add X", diff_text="d", findings=findings
            )
        finally:
            sup.read_changeset_entries = orig
        return captured.get("args", {}), verdict

    def test_the_open_findings_reach_the_reviewer(self):
        args, _ = self._drive_review(b'{"approved": true, "summary": "s"}', "- [abc123abc123] cache")
        self.assertEqual(args.get("findings"), "- [abc123abc123] cache")

    def test_a_resolved_list_is_read_off_the_verdict(self):
        _, verdict = self._drive_review(
            b'{"approved": false, "summary": "s", "issues": ["x"], "resolved": ["abc123abc123"]}',
            "",
        )
        self.assertEqual(verdict.resolved, ["abc123abc123"])

    def test_a_verdict_without_resolved_is_still_readable(self):
        # Every reviewer predating the field, and any that just omits it.
        _, verdict = self._drive_review(b'{"approved": true, "summary": "s"}', "")
        self.assertEqual(verdict.resolved, [])


class TheAttemptLabelNamesItsOwnBudget(unittest.TestCase):
    def test_a_single_round_run_prints_what_it_always_printed(self):
        from shepherd_dev.supervisor import _attempt_label

        self.assertEqual(_attempt_label(2, 3, round_no=1, review_rounds=1), "attempt 1/3")
        self.assertEqual(_attempt_label(0, 3, round_no=1, review_rounds=1), "attempt 3/3")

    def test_a_rework_round_counts_from_one_again(self):
        # The defect: the run-wide counter printed against a per-round budget,
        # so a fresh round's first attempt read as `attempt 2/2` — apparently
        # spent — and a third attempt would have read `attempt 3/2`.
        from shepherd_dev.supervisor import _attempt_label

        label = _attempt_label(1, 2, round_no=2, review_rounds=2)
        self.assertTrue(label.startswith("attempt 1/2"), label)
        self.assertIn("round 2/2", label)

    def test_the_printed_run_labels_the_rework_round_correctly(self):
        """The helper is only worth anything if the loop actually calls it —
        pinning the pure function alone would let the call sites be reverted
        to the run-wide counter without a single test noticing."""
        import io

        from shepherd_dev.progress import ProgressReporter

        buf = io.StringIO()
        DevelopReworkLoopTests()._run(
            verdicts=[(False, ["issue A"]), (False, ["issue A"])],
            review_rounds=2,
            max_attempts=2,
            reporter=ProgressReporter(stream=buf, enabled=False),
        )
        out = buf.getvalue()
        self.assertIn("attempt 1/2 · worker running", out)      # round 1
        self.assertIn("attempt 1/2 · round 2/2 · worker running", out)
        self.assertNotIn("attempt 2/2 · worker running", out)   # the old lie

    def test_the_numerator_never_exceeds_the_budget(self):
        from shepherd_dev.supervisor import _attempt_label

        for max_attempts in (1, 2, 3):
            for round_no in (1, 2, 3):
                for spent in range(1, max_attempts + 1):
                    label = _attempt_label(max_attempts - spent, max_attempts, round_no, 3)
                    with self.subTest(label=label):
                        position = int(label.split()[1].split("/")[0])
                        self.assertLessEqual(position, max_attempts)
                        self.assertGreaterEqual(position, 1)


if __name__ == "__main__":
    unittest.main()
