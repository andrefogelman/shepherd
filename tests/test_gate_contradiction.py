"""A rejection that contradicts the gate is flagged, not silently believed.

Seed: run-64aad3fcac0f. The reviewer rejected with "leaving marcar_critico/3
permanently unclosed … fails with 'missing terminator: end'". The gate had
already compiled that exact proposal and run 952 tests green — Elixir compiles
before it tests, so an unclosed module produces no "Generated sac app" and no
passing test.

That contradiction is mechanical: the gate's own result is in hand when the
verdict arrives. This does not overrule the reviewer — accusing is its job and
deciding is the human's — it appends the fact, so nobody has to notice it
unaided.

Runnable with: python -m unittest tests.test_gate_contradiction
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SEED_ISSUE = (
    "the second hunk inserts the new escalar/3 function between the end that closes "
    "marcar_critico/3's Repo.transaction and the single pre-existing context line "
    "'end' ... leaving marcar_critico/3 permanently unclosed. I reconstructed the "
    "patched file exactly as the diff specifies and ran it through "
    "Code.string_to_quoted: it fails with 'missing terminator: end'"
)


class BuildClaimDetectionTests(unittest.TestCase):
    def test_the_seed_issue_is_recognised(self):
        from shepherd_dev.supervisor import _claims_it_does_not_build

        self.assertTrue(_claims_it_does_not_build([SEED_ISSUE]))

    def test_the_common_phrasings_are_recognised(self):
        from shepherd_dev.supervisor import _claims_it_does_not_build

        for text in (
            "this does not compile",
            "SyntaxError: unexpected token",
            "the module is left unclosed",
            "missing terminator: end",
            "it will not build",
        ):
            with self.subTest(text=text):
                self.assertTrue(_claims_it_does_not_build([text]))

    def test_ordinary_findings_are_not(self):
        from shepherd_dev.supervisor import _claims_it_does_not_build

        for text in (
            "the new function has no test",
            "this leaks a token into the log",
            "naming does not match the surrounding module",
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(_claims_it_does_not_build([text]))


class ContradictionIsAppendedTests(unittest.TestCase):
    def _verdict(self, *, approved, issues, gate):
        from shepherd_dev.supervisor import ReviewVerdict, flag_gate_contradiction

        return flag_gate_contradiction(
            ReviewVerdict(approved=approved, summary="s", issues=list(issues)), gate
        )

    def _gate(self, passed, tail="Compiling 14 files (.ex)\nGenerated sac app\n952 tests, 0 failures"):
        from shepherd_dev.supervisor import GateResult

        return GateResult(passed, 0 if passed else 1, tail)

    def test_a_build_rejection_against_a_passing_gate_is_flagged(self):
        v = self._verdict(approved=False, issues=[SEED_ISSUE], gate=self._gate(True))
        self.assertEqual(len(v.issues), 2, "the original finding is kept, not replaced")
        self.assertIn(SEED_ISSUE, v.issues)
        self.assertTrue(
            any("gate" in i.lower() and "contradic" in i.lower() for i in v.issues), v.issues
        )

    def test_the_verdict_is_not_overturned(self):
        """Flagging is not deciding. A rejection stays a rejection."""
        v = self._verdict(approved=False, issues=[SEED_ISSUE], gate=self._gate(True))
        self.assertFalse(v.approved)

    def test_a_failing_gate_is_no_contradiction(self):
        v = self._verdict(approved=False, issues=[SEED_ISSUE], gate=self._gate(False))
        self.assertEqual(v.issues, [SEED_ISSUE])

    def test_no_gate_at_all_is_no_contradiction(self):
        """--no-review paths and the parallel coordinator run without one."""
        v = self._verdict(approved=False, issues=[SEED_ISSUE], gate=None)
        self.assertEqual(v.issues, [SEED_ISSUE])

    def test_an_ordinary_rejection_is_untouched(self):
        v = self._verdict(approved=False, issues=["no test for the new branch"], gate=self._gate(True))
        self.assertEqual(v.issues, ["no test for the new branch"])

    def test_an_approval_is_untouched(self):
        v = self._verdict(approved=True, issues=[], gate=self._gate(True))
        self.assertEqual(v.issues, [])

    def test_an_errored_verdict_is_untouched(self):
        from shepherd_dev.supervisor import ReviewVerdict, flag_gate_contradiction

        v = flag_gate_contradiction(
            ReviewVerdict(approved=False, summary="", error="review unavailable"), self._gate(True)
        )
        self.assertEqual(v.issues, [])

    def test_the_note_quotes_the_gate_so_it_can_be_checked(self):
        v = self._verdict(approved=False, issues=[SEED_ISSUE], gate=self._gate(True))
        note = next(i for i in v.issues if i != SEED_ISSUE)
        self.assertIn("952 tests, 0 failures", note)


if __name__ == "__main__":
    unittest.main()
