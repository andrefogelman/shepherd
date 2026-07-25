"""Tests for the authority invariant of the rework loop (phase 5).

A pre-approved bounded allowance is delegated authority; a loop that widens its
own authority to get past an objection is not. The dangerous version of rework
is the one where round 2 quietly gets a bigger budget or a wider path scope than
round 1 was granted — the reviewer's objection would then be answered by editing
files the human never allowed. Every round is judged against the same policy
object, with the same per-round allowance, or the bound means nothing.
Runnable with: python -m unittest tests.test_authority_invariant
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
class ReworkAuthorityTests(unittest.TestCase):
    """develop() driven by fakes: the substrate is stubbed, the loop is real."""

    def _drive(self, *, proposals, verdicts, policy=None, review_rounds=2,
               max_attempts=2, gate_passes=None):
        """proposals: the changeset each worker attempt hands back, in order
        (the last one repeats if the loop asks for more)."""
        from shepherd_dev import supervisor as sup

        calls: dict = {"worker": [], "review": 0, "gates": 0, "policy": []}

        class _Output:
            def __init__(self, n):
                self.n = n

            def changeset(self):
                return dict(proposals[min(self.n, len(proposals)) - 1])

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
                calls["worker"].append(kw)
                return _Run(len(calls["worker"]))

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

        real_check_paths = sup.check_paths

        def _check_paths(paths, pol):
            calls["policy"].append(pol)
            return real_check_paths(paths, pol)

        orig = (sup.read_changeset_entries, sup._run_gate, sup.run_review,
                sup._start_gate_warmup, sup.check_paths)
        sup.read_changeset_entries = dict
        sup._run_gate = _gate
        sup.run_review = _review
        sup._start_gate_warmup = lambda *a, **k: None
        sup.check_paths = _check_paths
        try:
            report = sup.develop(
                _Workspace(), object(), repo=object(), repo_root=Path("/r"),
                feature="add X", test_cmd="pytest -q", review_task=object(),
                policy=policy, max_attempts=max_attempts, review_rounds=review_rounds,
            )
        finally:
            (sup.read_changeset_entries, sup._run_gate, sup.run_review,
             sup._start_gate_warmup, sup.check_paths) = orig
        return report, calls

    def _reworking_run(self, policy=None, **kw):
        """A run that passes the gate, is rejected, and spends a rework round."""
        return self._drive(
            proposals=[{"tests/a.py": b"v1\n"}, {"tests/a.py": b"v2\n"}],
            verdicts=[(False, ["issue A"]), (True, [])],
            policy=policy, **kw,
        )

    # -- the policy object itself ---------------------------------------------
    def test_the_policy_object_is_never_mutated_across_rounds(self):
        from shepherd_dev.policy import ChangesetPolicy

        policy = ChangesetPolicy(max_changed_paths=3, allowed_prefixes=("tests/",))
        before = (policy.max_changed_paths, policy.allowed_prefixes,
                  policy.forbidden_paths)
        report, calls = self._reworking_run(policy=policy)

        self.assertEqual(len(calls["worker"]), 2)  # the rework round really ran
        self.assertEqual(report.outcome, "passed_approved")
        self.assertEqual(policy.max_changed_paths, before[0])
        self.assertEqual(policy.allowed_prefixes, before[1])
        self.assertIs(policy.allowed_prefixes, before[1])  # not even rebuilt
        self.assertEqual(policy.forbidden_paths, before[2])

    def test_every_round_is_judged_against_the_same_policy_object(self):
        from shepherd_dev.policy import ChangesetPolicy

        policy = ChangesetPolicy(allowed_prefixes=("tests/",))
        _, calls = self._reworking_run(policy=policy)

        self.assertEqual(len(calls["policy"]), 2)  # one check per attempt
        for seen in calls["policy"]:
            self.assertIs(seen, policy)

    # -- the granted scope holds in a later round -----------------------------
    def test_a_rework_round_cannot_write_outside_the_allowed_prefixes(self):
        from shepherd_dev.policy import ChangesetPolicy

        report, calls = self._drive(
            proposals=[{"tests/a.py": b"v1\n"}, {"src/x.py": b"v2\n"}],
            verdicts=[(False, ["issue A"])],
            policy=ChangesetPolicy(allowed_prefixes=("tests/",)),
        )
        self.assertEqual(report.attempts[1].verdict, "policy_rejected")
        self.assertTrue(
            any("outside allowed prefixes" in v for v in report.attempts[1].policy_violations),
            report.attempts[1].policy_violations,
        )
        self.assertEqual(calls["review"], 1)  # the out-of-scope rework never reached review

    def test_a_rework_round_cannot_exceed_max_changed_paths(self):
        from shepherd_dev.policy import ChangesetPolicy

        report, _ = self._drive(
            proposals=[
                {"tests/a.py": b"v1\n"},
                {"tests/a.py": b"v2\n", "tests/b.py": b"v2\n"},
            ],
            verdicts=[(False, ["issue A"])],
            policy=ChangesetPolicy(max_changed_paths=1),
        )
        self.assertEqual(report.attempts[1].verdict, "policy_rejected")
        self.assertTrue(
            any("max 1" in v for v in report.attempts[1].policy_violations),
            report.attempts[1].policy_violations,
        )

    def test_a_rework_round_cannot_touch_a_forbidden_path(self):
        report, _ = self._drive(
            proposals=[{"tests/a.py": b"v1\n"}, {".env": b"SECRET=1\n"}],
            verdicts=[(False, ["issue A"])],
        )
        self.assertEqual(report.attempts[1].verdict, "policy_rejected")
        self.assertTrue(
            any("forbidden" in v for v in report.attempts[1].policy_violations),
            report.attempts[1].policy_violations,
        )

    # -- what the worker is handed --------------------------------------------
    def test_a_rework_round_runs_the_worker_under_the_same_confinement(self):
        _, calls = self._reworking_run()

        self.assertEqual(len(calls["worker"]), 2)
        first, second = calls["worker"]
        # Only the feedback may differ between rounds; everything that bounds
        # the worker — where it runs, what it runs as, which repo — may not.
        for key in ("placement", "runtime", "repo"):
            self.assertEqual(first[key], second[key], key)
        self.assertEqual(first["feature"], second["feature"])
        self.assertNotEqual(first["guidance"], second["guidance"])

    # -- the allowance ---------------------------------------------------------
    def test_a_round_never_raises_the_per_round_attempt_allowance(self):
        # Round 1 passes and is rejected; every attempt of round 2 then fails
        # the gate. Round 2 gets its own max_attempts — and not one more.
        report, calls = self._drive(
            proposals=[{"tests/a.py": b"v1\n"}, {"tests/a.py": b"v2\n"},
                       {"tests/a.py": b"v3\n"}, {"tests/a.py": b"v4\n"}],
            verdicts=[(False, ["issue A"])],
            gate_passes=[True, False],
            review_rounds=2, max_attempts=2,
        )
        self.assertEqual(len(calls["worker"]), 3)  # 1 in round 1 + 2 in round 2
        self.assertEqual(report.outcome, "failed")

    def test_rounds_do_not_multiply_the_worker_beyond_their_own_budgets(self):
        # Nothing the loop does may exceed review_rounds × max_attempts workers.
        report, calls = self._drive(
            proposals=[{"tests/a.py": f"v{i}\n".encode()} for i in range(1, 12)],
            verdicts=[(False, ["issue A"])],
            review_rounds=3, max_attempts=2,
        )
        self.assertLessEqual(len(calls["worker"]), 3 * 2)
        self.assertEqual(report.outcome, "passed_rejected")


if __name__ == "__main__":
    unittest.main()
