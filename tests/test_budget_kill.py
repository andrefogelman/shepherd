"""A worker reaped at its budget is a timeout, and says so.

The launch perl exits 124 on the alarm; the substrate wraps that as
`ProviderInvocationError("confined body refused (rc=124): …")`, and the
framework's own alarm raises `BudgetExhausted`. Both were recorded as a
generic `run_failed` — 13 attempts of ~915 s in the history, 3.3 hours whose
cause the record did not name — and the timeout guidance never reached the
retry.

Runnable with: python -m unittest tests.test_budget_kill
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.supervisor import _TIMEOUT_GUIDANCE, _budget_killed  # noqa: E402


class Recognition(unittest.TestCase):
    def test_the_killtree_exit_code_is_a_budget_kill(self):
        exc = RuntimeError(
            "RunStartError: run run-194a48a20751 failed: ProviderInvocationError: "
            'confined body refused (rc=124): stamp":"2026-07-13T10:54:30.345Z"…'
        )
        self.assertTrue(_budget_killed(exc))

    def test_the_frameworks_own_exception_is_too(self):
        class BudgetExhausted(Exception):
            pass

        self.assertTrue(_budget_killed(BudgetExhausted("max turns reached")))
        self.assertTrue(_budget_killed(RuntimeError("budget exceeded (240s): no output before the alarm")))

    def test_other_failures_are_not(self):
        self.assertFalse(_budget_killed(RuntimeError("confined body refused (rc=1): Not logged in")))
        self.assertFalse(_budget_killed(RuntimeError("You've hit your weekly limit")))
        self.assertFalse(_budget_killed(RuntimeError("rc=1240 is not 124")))


class DevelopRecordsATimeout(unittest.TestCase):
    def _run(self, exc):
        from shepherd_dev import supervisor as sup

        seen: list[str] = []

        class _Tasks:
            def register(self, task):
                pass

        class _Workspace:
            tasks = _Tasks()
            calls = 0

            def run(self, task, **kw):
                seen.append(kw.get("guidance", ""))
                self.calls += 1
                if self.calls == 1:
                    raise exc
                raise RuntimeError("second attempt: stop here")

        orig = sup._start_gate_warmup
        sup._start_gate_warmup = lambda *a, **k: None
        try:
            report = sup.develop(
                _Workspace(), object(), repo="R", repo_root=Path("/r"), feature="f",
                test_cmd="true", max_attempts=2, worker_budget=900,
            )
        finally:
            sup._start_gate_warmup = orig
        return report, seen

    def test_rc_124_becomes_timed_out_with_the_timeout_guidance(self):
        report, seen = self._run(RuntimeError("ProviderInvocationError: confined body refused (rc=124): …"))
        first = report.attempts[0]
        self.assertEqual(first.verdict, "timed_out")
        self.assertIn("900", first.error)
        self.assertIn("hard-killed", first.error)
        self.assertEqual(seen[1], _TIMEOUT_GUIDANCE)

    def test_an_ordinary_failure_still_reads_run_failed(self):
        report, seen = self._run(RuntimeError("something else entirely"))
        self.assertEqual(report.attempts[0].verdict, "run_failed")
        self.assertNotEqual(seen[1], _TIMEOUT_GUIDANCE)


class RepoLimits(unittest.TestCase):
    def test_flags_default_to_none_so_config_can_fill_them(self):
        from shepherd_dev.cli import build_parser

        for argv in (["run", "f"], ["run2", "a", "b"], ["runN", "a", "b"]):
            args = build_parser().parse_args(argv)
            self.assertIsNone(args.worker_budget, argv)
            self.assertIsNone(args.gate_timeout, argv)
            self.assertIsNone(args.max_attempts, argv)

    def test_config_over_default_and_flag_over_config(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch

        from shepherd_dev import config
        from shepherd_dev.cli import _apply_run_limits

        repo = Path(tempfile.mkdtemp(prefix="shepherd-limits-"))
        config.save_config(repo, {"limits": {"worker_budget": 1800, "max_attempts": 4, "gate_timeout": "no"}})
        with patch.object(config, "GLOBAL_CONFIG", repo / "no-global.json"):
            args = SimpleNamespace(worker_budget=None, gate_timeout=None, max_attempts=None)
            _apply_run_limits(args, repo, "run")
            self.assertEqual((args.worker_budget, args.gate_timeout, args.max_attempts), (1800, 600, 4))
            args = SimpleNamespace(worker_budget=300, gate_timeout=None, max_attempts=None)
            _apply_run_limits(args, repo, "run2")
            self.assertEqual((args.worker_budget, args.gate_timeout, args.max_attempts), (300, 600, 4))
            args = SimpleNamespace(worker_budget=None, gate_timeout=None, max_attempts=None)
            config.save_config(repo, {"limits": {}})
            _apply_run_limits(args, repo, "runN")
            self.assertEqual((args.worker_budget, args.gate_timeout, args.max_attempts), (900, 600, 2))

    def test_run_limits_ignores_what_a_flag_would_refuse(self):
        import tempfile
        from unittest.mock import patch

        from shepherd_dev import config

        repo = Path(tempfile.mkdtemp(prefix="shepherd-limits-"))
        config.save_config(repo, {"limits": {"worker_budget": 0, "gate_timeout": -5, "max_attempts": True}})
        with patch.object(config, "GLOBAL_CONFIG", repo / "no-global.json"):
            self.assertEqual(config.run_limits(repo), {})


if __name__ == "__main__":
    unittest.main()
