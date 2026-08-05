"""Tests for the findings ledger (phase 2 of the review-rounds work).

A reviewer's issues are the only record of what is still wrong, and today they
evaporate: the run ends, the prose reads like success, and the tally only
appears if a human interrogates it. The ledger gives every issue a stable id,
carries it verbatim across rounds, and lets it leave only through a terminal
state. Re-labelling a finding's severity is not a fix, so severity is stripped
before hashing and the id survives the re-label.
Runnable with: python -m unittest tests.test_ledger
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.ledger import Ledger, finding_id  # noqa: E402

try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


class FindingIdTests(unittest.TestCase):
    def test_stable_across_whitespace_and_case(self):
        a = finding_id("Operator can remove their own tenant links")
        b = finding_id("  operator can   remove their own TENANT links.  ")
        self.assertEqual(a, b)

    def test_different_problems_get_different_ids(self):
        self.assertNotEqual(
            finding_id("Operator can remove their own tenant links"),
            finding_id("Selector offers suspended tenants"),
        )

    def test_severity_relabel_does_not_create_a_new_finding(self):
        base = finding_id("Operator can remove their own tenant links")
        for prefix in (
            "HIGH: ",
            "MEDIUM: ",
            "HIGH -> MEDIUM: ",
            "ALTO: ",
            "MÉDIO: ",
            "ALTO→MÉDIO: ",
            "[BLOCKER] ",
            "(low) ",
            "BAIXO — ",
        ):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    finding_id(prefix + "Operator can remove their own tenant links"), base
                )

    def test_a_severity_word_inside_the_sentence_is_kept(self):
        # "high" here is part of the problem, not a label — stripping it would
        # collapse two genuinely different findings into one.
        self.assertNotEqual(
            finding_id("high cardinality index is never used"),
            finding_id("cardinality index is never used"),
        )

    def test_id_is_short_and_hex(self):
        fid = finding_id("something is wrong")
        self.assertEqual(len(fid), 12)
        int(fid, 16)


class LedgerRoundTests(unittest.TestCase):
    def test_first_round_opens_every_issue(self):
        led = Ledger()
        led.record_round(1, ["issue A", "issue B"])
        self.assertEqual(len(led.findings), 2)
        self.assertTrue(all(f.state == "open" for f in led.findings))
        self.assertEqual([f.rounds for f in led.findings], [[1], [1]])

    def test_reraised_issue_stays_one_finding_with_a_round_trail(self):
        led = Ledger()
        led.record_round(1, ["issue A"])
        led.record_round(2, ["HIGH: issue A"])
        self.assertEqual(len(led.findings), 1)
        self.assertEqual(led.findings[0].rounds, [1, 2])
        self.assertEqual(led.findings[0].state, "open")

    def test_issue_absent_from_a_rejection_stays_open(self):
        # A rejecting reviewer that stops mentioning an item has not said it is
        # fixed — most often it reworded it. Silence closes nothing.
        led = Ledger()
        led.record_round(1, ["issue A", "issue B"])
        led.record_round(2, ["issue B"])
        by_id = {f.id: f for f in led.findings}
        self.assertEqual(by_id[finding_id("issue A")].state, "open")
        self.assertEqual(by_id[finding_id("issue B")].state, "open")

    def test_a_closed_finding_that_comes_back_reopens(self):
        led = Ledger()
        led.record_round(1, ["issue A"])
        led.record_round(2, [], approved=True)
        self.assertEqual(led.findings[0].state, "accepted")
        led.record_round(3, ["issue A"])
        self.assertEqual(led.findings[0].state, "open")
        self.assertEqual(led.findings[0].rounds, [1, 3])

    def test_terminal_states_are_not_reopened_by_a_later_review(self):
        led = Ledger()
        led.record_round(1, ["issue A"])
        led.close(finding_id("issue A"), "blocked", reason="needs a backend change")
        led.record_round(2, ["issue A"])
        self.assertEqual(led.findings[0].state, "blocked")
        self.assertEqual(led.findings[0].reason, "needs a backend change")


class LedgerClosureTests(unittest.TestCase):
    def test_blocked_demands_a_reason(self):
        led = Ledger()
        led.record_round(1, ["issue A"])
        with self.assertRaises(ValueError):
            led.close(finding_id("issue A"), "blocked")

    def test_refused_demands_a_reason(self):
        led = Ledger()
        led.record_round(1, ["issue A"])
        with self.assertRaises(ValueError):
            led.close(finding_id("issue A"), "refused", reason="   ")

    def test_unknown_state_is_refused(self):
        led = Ledger()
        led.record_round(1, ["issue A"])
        with self.assertRaises(ValueError):
            led.close(finding_id("issue A"), "downgraded", reason="now medium")

    def test_unknown_id_is_refused(self):
        led = Ledger()
        with self.assertRaises(KeyError):
            led.close("deadbeefcafe", "refused", reason="not mine")

    def test_open_findings_and_has_open(self):
        led = Ledger()
        led.record_round(1, ["issue A", "issue B"])
        self.assertTrue(led.has_open())
        led.close(finding_id("issue A"), "refused", reason="human accepts the risk")
        self.assertEqual([f.text for f in led.open_findings()], ["issue B"])
        led.record_round(2, [], approved=True)
        self.assertFalse(led.has_open())


class ClosureNeedsEvidenceTests(unittest.TestCase):
    """A finding closes on evidence, never on the reviewer having gone quiet.

    Closing by absence assumes the later reviewer knew what the earlier one
    said. It does not, unless it is told — and being a language model it
    restates the same objection in new words, which reads as one item fixed
    plus one new item found.
    """

    #: The pair that exposed it, from a real run: same objection, same rule,
    #: two roundings of the words. Kept verbatim so a future normalization
    #: that claims to handle rewording is measured against the real thing.
    ROUND_1 = (
        'CONVENTIONS.md "Each module ships its own test file": new module '
        "duration.py is not accompanied by a test file exercising every "
        "documented behavior."
    )
    ROUND_2 = (
        "BLOCKING — CONVENTIONS.md 'Each module ships its own test file': new "
        "module duration.py is not covered by tests."
    )

    def test_the_observed_reword_does_not_read_as_one_fixed_plus_one_new(self):
        led = Ledger()
        led.record_round(1, [self.ROUND_1])
        led.record_round(2, [self.ROUND_2])
        self.assertEqual(
            [f.state for f in led.findings],
            ["open", "open"],
            "a reworded objection closed the original as fixed",
        )

    def test_a_finding_re_raised_by_id_stays_one_finding(self):
        # The mechanism that makes the round trail true: the reviewer is handed
        # the open findings with their ids and points at one instead of
        # describing it again.
        led = Ledger()
        led.record_round(1, [self.ROUND_1])
        fid = finding_id(self.ROUND_1)
        led.record_round(2, [f"[{fid}] {self.ROUND_2}"])
        self.assertEqual(len(led.findings), 1)
        self.assertEqual(led.findings[0].state, "open")
        self.assertEqual(led.findings[0].rounds, [1, 2])

    def test_a_finding_the_reviewer_reports_resolved_is_fixed(self):
        led = Ledger()
        led.record_round(1, ["issue A", "issue B"])
        led.record_round(2, ["issue B"], resolved=[finding_id("issue A")])
        by_id = {f.id: f for f in led.findings}
        self.assertEqual(by_id[finding_id("issue A")].state, "fixed")
        self.assertEqual(by_id[finding_id("issue B")].state, "open")

    def test_approval_closes_whatever_is_still_open_as_accepted(self):
        # Approval IS the explicit judgement: the reviewer saw the open list
        # and signed the change off anyway. But it closes them `accepted`, not
        # `fixed` — signing the change off says nothing about whether THIS
        # item was dealt with, and reporting it as fixed claimed work nobody
        # did (findings read [fixed] while still in the delivered file).
        led = Ledger()
        led.record_round(1, ["issue A", "issue B"])
        led.record_round(2, [], approved=True)
        self.assertEqual({f.state for f in led.findings}, {"accepted"})

    def test_only_an_explicit_resolved_earns_fixed(self):
        led = Ledger()
        led.record_round(1, ["issue A", "issue B"])
        led.record_round(2, [], resolved=[finding_id("issue A")], approved=True)
        by_id = {f.id: f.state for f in led.findings}
        self.assertEqual(by_id[finding_id("issue A")], "fixed")     # checked
        self.assertEqual(by_id[finding_id("issue B")], "accepted")  # merely approved

    def test_a_re_raise_beats_a_resolved_claim_for_the_same_finding(self):
        # Contradictory verdict; the unsafe reading is the one that closes.
        led = Ledger()
        led.record_round(1, ["issue A"])
        fid = finding_id("issue A")
        led.record_round(2, [f"[{fid}] still there"], resolved=[fid])
        self.assertEqual(led.findings[0].state, "open")

    def test_resolved_does_not_overwrite_a_terminal_finding(self):
        led = Ledger()
        led.record_round(1, ["issue A"])
        fid = finding_id("issue A")
        led.close(fid, "refused", reason="human accepts the risk")
        led.record_round(2, [], resolved=[fid])
        self.assertEqual(led.findings[0].state, "refused")

    def test_an_unknown_resolved_id_is_ignored(self):
        led = Ledger()
        led.record_round(1, ["issue A"])
        led.record_round(2, ["issue A"], resolved=["deadbeefcafe"])
        self.assertEqual(led.findings[0].state, "open")

    def test_an_unknown_id_prefix_falls_back_to_the_text_identity(self):
        # A hallucinated id must not mint a finding keyed on the hallucination,
        # or the same text next round becomes yet another new item.
        led = Ledger()
        led.record_round(1, ["[deadbeefcafe] issue A"])
        self.assertEqual(led.findings[0].id, finding_id("issue A"))
        led.record_round(2, ["issue A"])
        self.assertEqual(len(led.findings), 1)


class LedgerRenderTests(unittest.TestCase):
    def test_render_shows_state_round_trail_and_reason(self):
        led = Ledger()
        led.record_round(1, ["issue A", "issue B"])
        led.record_round(2, ["issue B"], resolved=[finding_id("issue A")])
        led.close(finding_id("issue B"), "blocked", reason="needs a backend change")
        text = led.render()
        self.assertIn("issue A", text)
        self.assertIn("fixed", text)
        self.assertIn("blocked", text)
        self.assertIn("needs a backend change", text)
        self.assertIn("rounds 1-2", text)

    def test_render_is_empty_without_findings(self):
        self.assertEqual(Ledger().render(), "")

    def test_payload_is_json_serializable_and_round_trips(self):
        led = Ledger()
        led.record_round(1, ["issue A"])
        led.close(finding_id("issue A"), "blocked", reason="upstream")
        payload = led.to_payload()
        json.dumps(payload)
        back = Ledger.from_payload(payload)
        self.assertEqual(back.findings[0].id, led.findings[0].id)
        self.assertEqual(back.findings[0].state, "blocked")
        self.assertEqual(back.findings[0].reason, "upstream")
        self.assertEqual(back.findings[0].rounds, [1])


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class LedgerOnTheReportTests(unittest.TestCase):
    def _report_with_ledger(self):
        from shepherd_dev.supervisor import DevReport, ReviewVerdict

        report = DevReport(feature="add X", succeeded=True, repo="/r")
        report.final_run_ref = "run-abc"
        report.review = ReviewVerdict(approved=False, summary="two problems", issues=["i1", "i2"])
        report.ledger = Ledger()
        report.ledger.record_round(1, report.review.issues)
        return report

    def test_default_report_has_no_ledger(self):
        from shepherd_dev.supervisor import DevReport

        self.assertIsNone(DevReport(feature="f", succeeded=False).ledger)

    def test_summary_prints_the_ledger_unprompted(self):
        text = self._report_with_ledger().summary()
        self.assertIn("findings:", text)
        self.assertIn("i1", text)
        self.assertIn("i2", text)

    def test_payload_carries_the_ledger(self):
        from shepherd_dev import history

        payload = history.run_payload(
            self._report_with_ledger(), Path("/r"),
            mode="feature", test_cmd="pytest -q", provider="claude", flags={},
        )
        self.assertEqual(len(payload["findings"]), 2)
        self.assertEqual(payload["findings"][0]["state"], "open")
        json.dumps(payload)

    def test_envelope_carries_the_ledger(self):
        from shepherd_dev.cli import _report_envelope

        env = _report_envelope(
            self._report_with_ledger(), repo_root=Path("/r"), mode="feature",
            test_cmd="pytest -q", provider="claude",
        )
        self.assertEqual(len(env["findings"]), 2)
        json.dumps(env)

    def test_payload_findings_is_empty_without_a_ledger(self):
        from shepherd_dev import history
        from shepherd_dev.supervisor import DevReport

        payload = history.run_payload(
            DevReport(feature="f", succeeded=True), Path("/r"),
            mode="feature", test_cmd=None, provider="claude", flags={},
        )
        self.assertEqual(payload["findings"], [])


if __name__ == "__main__":
    unittest.main()
