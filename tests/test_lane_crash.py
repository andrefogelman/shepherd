"""A worker thread that raises must surface as a report, never as a raise.

develop_many already contained a lane crash ("a lane crash must not sink the
others"); develop_parallel (run2) and develop_best_of collected their futures
with a bare `[f.result() for f in futures]`, so the first worker to raise
propagated the exception straight out of the call — no ParallelReport, no
BestOfReport, and in best-of the other K-1 candidates were thrown away with it.

Runnable with: python -m unittest tests.test_lane_crash
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


class _Boom(RuntimeError):
    pass


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class WorkerCrashContainment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-crash-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("A = 1\n")

    def _patch(self, fake_worker):
        """Stub the worker + clone so no substrate run happens."""
        from shepherd_dev import parallel as P

        old = (P._run_worker, P._clone_workspace, P._clone_many)
        P._run_worker = fake_worker
        P._clone_workspace = lambda repo_root, overlay=None: self.repo
        P._clone_many = lambda repo_root, n: [self.repo] * n

        def _restore():
            P._run_worker, P._clone_workspace, P._clone_many = old

        self.addCleanup(_restore)
        return P

    def _ok_report(self, feature, clone, entries):
        from shepherd_dev.supervisor import DevReport

        report = DevReport(feature=feature, succeeded=True, repo=str(clone))
        report.entries = entries
        report.final_run_ref = f"run-{feature}"
        return report

    def test_run2_worker_crash_returns_a_report(self):
        def fake(clone, feature, note, **_kw):
            if feature == "crashy":
                raise _Boom("worker blew up")
            return self._ok_report(feature, clone, {"src/b.py": b"B = 1\n"})

        P = self._patch(fake)
        report = P.develop_parallel(
            self.repo, ["healthy", "crashy"], test_cmd="true", provider="static"
        )
        self.assertFalse(report.succeeded)
        self.assertIsNotNone(report.error)
        self.assertIn("_Boom", report.error or "")
        self.assertIn("crashy", report.error or "")
        # the surviving worker's result is still on the report, not discarded
        self.assertEqual(len(report.workers), 1)

    def test_best_of_crash_does_not_sink_the_other_candidates(self):
        import threading

        lock = threading.Lock()
        calls = {"n": 0}

        def counting(clone, feature, note, **_kw):
            with lock:
                calls["n"] += 1
                mine = calls["n"]
            if mine == 1:  # exactly one candidate blows up
                raise _Boom("candidate blew up")
            return self._ok_report(feature, clone, {f"src/c{mine}.py": b"C = 1\n"})

        P = self._patch(counting)
        report = P.develop_best_of(
            self.repo, "feat", k=3, test_cmd="true", provider="static"
        )
        self.assertEqual(len(report.candidates), 3)
        crashed = [c for c in report.candidates if "_Boom" in c.verdict]
        self.assertEqual(len(crashed), 1)
        self.assertFalse(crashed[0].succeeded)
        # the other two still gated and ranked
        self.assertEqual(len([c for c in report.candidates if c.succeeded]), 2)


if __name__ == "__main__":
    unittest.main()
