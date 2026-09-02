"""A finding has a severity, and only a blocking one rejects.

Measured on 100 real verdicts: the reviewer rejected 58, the human then
accepted 34 of those 58 — because "Minor/non-blocking: …" and "Nit: …" landed
in `issues` and any issue meant `approved: false`. A rejection nobody would
act on is not a verdict; it is noise that disables --auto-settle and spends a
rework round on a naming quibble.

Runnable with: python -m unittest tests.test_review_severity
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.ledger import severity_of  # noqa: E402
from shepherd_dev.supervisor import (  # noqa: E402
    DevReport,
    ReviewVerdict,
    _aggregate_review_verdicts,
    parse_review_issues,
    render_review_report,
    verdict_from_review_json,
)


class SeverityOfPlainText(unittest.TestCase):
    def test_labels_that_mean_advisory(self):
        for text in (
            "Minor/non-blocking: prefer defp here",
            "Nit: trailing whitespace",
            "LOW — the log line could be shorter",
            "[minor] rename x",
            "non-blocking: consider a docstring",
            "Suggestion: extract a helper",
            "Opcional: mover para módulo próprio",
        ):
            with self.subTest(text=text):
                self.assertEqual(severity_of(text), "advisory")

    def test_everything_else_blocks(self):
        for text in (
            "HIGH: SQL built by string concatenation",
            "BLOCKING — marcar_critico/3 never closes",
            "the cache is never invalidated",
            "medium: race between two writers",
            "This is a minor module but the lock is wrong",  # 'minor' not a label
        ):
            with self.subTest(text=text):
                self.assertEqual(severity_of(text), "blocking")

    def test_an_id_prefix_does_not_hide_the_label(self):
        self.assertEqual(severity_of("[a1b2c3d4e5f6] nit: spacing"), "advisory")
        self.assertEqual(severity_of("[a1b2c3d4e5f6] the lock is wrong"), "blocking")


class ParseReviewIssues(unittest.TestCase):
    def test_objects_are_split_by_their_severity(self):
        blocking, advisory = parse_review_issues([
            {"severity": "blocking", "text": "lock released early"},
            {"severity": "advisory", "text": "rename foo"},
            {"severity": "ADVISORY", "text": "case does not matter"},
        ])
        self.assertEqual(blocking, ["lock released early"])
        self.assertEqual(advisory, ["rename foo", "case does not matter"])

    def test_an_unknown_or_missing_severity_fails_closed(self):
        blocking, advisory = parse_review_issues([
            {"severity": "whatever", "text": "a"},
            {"text": "b"},
        ])
        self.assertEqual(blocking, ["a", "b"])
        self.assertEqual(advisory, [])

    def test_strings_are_classified_by_their_label(self):
        blocking, advisory = parse_review_issues(["Minor: x", "y is wrong", "nit: z"])
        self.assertEqual(blocking, ["y is wrong"])
        self.assertEqual(advisory, ["Minor: x", "nit: z"])

    def test_empty_items_are_dropped(self):
        self.assertEqual(parse_review_issues(["", {"text": ""}, None, {"severity": "blocking"}]), ([], []))
        self.assertEqual(parse_review_issues(None), ([], []))


class VerdictFromJson(unittest.TestCase):
    def test_a_rejection_with_a_blocking_finding_stays_rejected(self):
        v = verdict_from_review_json({
            "approved": False, "summary": "no",
            "issues": [{"severity": "blocking", "text": "wrong"}, {"severity": "advisory", "text": "nit"}],
        })
        self.assertFalse(v.approved)
        self.assertEqual(v.issues, ["wrong"])
        self.assertEqual(v.advisories, ["nit"])

    def test_a_rejection_with_only_advisories_is_recorded_as_approved_and_says_so(self):
        v = verdict_from_review_json({
            "approved": False, "summary": "mostly fine",
            "issues": [{"severity": "advisory", "text": "rename foo"}, "Minor: spacing"],
        })
        self.assertTrue(v.approved)
        self.assertEqual(v.issues, [])
        self.assertEqual(v.advisories, ["rename foo", "Minor: spacing"])
        self.assertIn("mostly fine", v.summary)
        self.assertIn("listed no blocking finding", v.summary)

    def test_a_rejection_with_no_findings_at_all_stays_a_rejection(self):
        # Nothing named means nothing to correct with: it reaches the human as
        # "rejected but left no actionable finding", which is what happened.
        v = verdict_from_review_json({"approved": False, "summary": "meh", "issues": []})
        self.assertFalse(v.approved)
        self.assertNotIn("[shepherd]", v.summary)

    def test_a_malformed_approved_is_never_corrected_into_a_yes(self):
        for raw in ("false", "true", 1, 0, None, [], {}):
            with self.subTest(raw=raw):
                v = verdict_from_review_json({
                    "approved": raw, "summary": "s",
                    "issues": [{"severity": "advisory", "text": "nit"}],
                })
                self.assertFalse(v.approved, f"{raw!r} must not approve")

    def test_an_approval_alongside_a_blocking_finding_is_not_overturned(self):
        v = verdict_from_review_json({
            "approved": True, "summary": "ok", "issues": [{"severity": "blocking", "text": "hmm"}],
        })
        self.assertTrue(v.approved)
        self.assertEqual(v.issues, ["hmm"])

    def test_approved_is_the_json_literal_true_only(self):
        v = verdict_from_review_json({"approved": "true", "summary": "", "issues": [{"severity": "blocking", "text": "x"}]})
        self.assertFalse(v.approved)

    def test_resolved_ids_pass_through(self):
        v = verdict_from_review_json({"approved": True, "summary": "", "issues": [], "resolved": ["abc"]})
        self.assertEqual(v.resolved, ["abc"])


class Aggregation(unittest.TestCase):
    def test_advisories_are_unioned_and_never_reject(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="a", advisories=["n1", "n2"]),
            ReviewVerdict(approved=True, summary="b", advisories=["n2", "n3"]),
        ])
        self.assertTrue(v.approved)
        self.assertEqual(v.advisories, ["n1", "n2", "n3"])
        self.assertEqual(v.issues, [])


class Rendering(unittest.TestCase):
    def _report(self, review):
        report = DevReport(feature="f", succeeded=True, repo="/r")
        report.review = review
        return report

    def test_summary_and_report_show_both_lists_apart(self):
        review = ReviewVerdict(approved=True, summary="fine", issues=["must fix"], advisories=["could rename"])
        summary = self._report(review).summary()
        self.assertIn("  issue: must fix", summary)
        self.assertIn("  advisory: could rename", summary)
        report = render_review_report(self._report(review))
        self.assertIn("Issues (blocking):\n- must fix", report)
        self.assertIn("Advisory (not blocking):\n- could rename", report)

    def test_history_payload_carries_advisories(self):
        from shepherd_dev.history import review_payload

        payload = review_payload(ReviewVerdict(approved=True, summary="s", issues=["i"], advisories=["a"]))
        self.assertEqual(payload["issues"], ["i"])
        self.assertEqual(payload["advisories"], ["a"])


class ThePromptAsksForSeverity(unittest.TestCase):
    def test_the_review_prompt_defines_both_severities_and_binds_approved_to_them(self):
        from shepherd_dev.prompts import get_prompt

        text = get_prompt("review")
        self.assertIn('"severity": "blocking" | "advisory"', text)
        self.assertIn("MUST be true when no", text)
        self.assertIn("Do not inflate", text)


class TheCodexReviewerSpeaksTheSameShape(unittest.TestCase):
    def test_objects_and_the_advisory_correction(self):
        from shepherd_dev.providers.codex_host import _parse_verdict, _review_prompt

        v = _parse_verdict('{"approved": false, "summary": "s", "issues": [{"severity": "advisory", "text": "nit"}]}')
        self.assertIsNotNone(v)
        self.assertTrue(v.approved)
        self.assertEqual(v.advisories, ["nit"])
        self.assertIn('"severity": "blocking"|"advisory"', _review_prompt({"a.py": b""}, "f"))


class DevelopReworksOnlyOnBlockingFindings(unittest.TestCase):
    """The ledger tracks blocking findings; an advisory-only verdict approves
    the proposal outright, leaves the ledger empty and spends no rework."""

    def _run(self, verdicts, *, review_rounds=2):
        from shepherd_dev import supervisor as sup

        calls = {"worker": 0, "review": 0}

        class _Output:
            def __init__(self, n):
                self.n = n

            def changeset(self):
                # Different bytes per attempt: identical proposals trip the
                # no-progress guard, which is not what this test is about.
                return {"file.py": f"v{self.n}\n".encode()}

            def discard(self):
                pass

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

        def _review(workspace, review_task, **kw):
            i = calls["review"]
            calls["review"] += 1
            return verdicts[min(i, len(verdicts) - 1)]

        orig = (sup.read_changeset_entries, sup._run_gate, sup.run_review, sup._start_gate_warmup)
        sup.read_changeset_entries = lambda cs: dict(cs)
        sup._run_gate = lambda *a, **k: sup.GateResult(True, 0, "ok")
        sup.run_review = _review
        sup._start_gate_warmup = lambda *a, **k: None
        try:
            report = sup.develop(
                _Workspace(), object(), repo="R", repo_root=Path("/r"), feature="f",
                test_cmd="true", review_task=object(), max_attempts=1, review_rounds=review_rounds,
            )
        finally:
            sup.read_changeset_entries, sup._run_gate, sup.run_review, sup._start_gate_warmup = orig
        return report, calls

    def test_advisory_only_verdict_approves_without_a_rework(self):
        report, calls = self._run([
            ReviewVerdict(approved=True, summary="fine", issues=[], advisories=["rename foo"]),
        ])
        self.assertEqual(report.outcome, "passed_approved")
        self.assertEqual(calls["worker"], 1)
        self.assertEqual(calls["review"], 1)
        self.assertEqual(report.ledger.findings, [])
        self.assertEqual(report.review.advisories, ["rename foo"])

    def test_a_blocking_finding_still_drives_a_rework(self):
        report, calls = self._run([
            ReviewVerdict(approved=False, summary="no", issues=["lock wrong"], advisories=["nit"]),
            ReviewVerdict(approved=True, summary="ok", issues=[], resolved=[]),
        ])
        self.assertEqual(calls["worker"], 2)
        self.assertEqual(report.outcome, "passed_approved")
        texts = [f.text for f in report.ledger.findings]
        self.assertEqual(texts, ["lock wrong"])


if __name__ == "__main__":
    unittest.main()
