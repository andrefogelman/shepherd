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


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class BestOfPipelining(unittest.TestCase):
    """A1: best-of ran K x (gate, review) strictly in series. The gate is
    CPU-bound and must stay serialized; the review is network-bound and has no
    reason to wait for the next candidate's gate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-bestof-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("A = 1\n")

    def _run(self, k=3, review_delay=0.4, gate_delay=0.2):
        import threading
        import time

        from shepherd_dev import parallel as P
        from shepherd_dev.supervisor import DevReport, GateResult, ReviewVerdict

        counters = {"gates_now": 0, "gates_max": 0, "reviews_now": 0, "reviews_max": 0}
        lock = threading.Lock()
        seq = {"n": 0}

        def track(key, delay, make):
            with lock:
                counters[f"{key}_now"] += 1
                counters[f"{key}_max"] = max(counters[f"{key}_max"], counters[f"{key}_now"])
            try:
                time.sleep(delay)
                return make()
            finally:
                with lock:
                    counters[f"{key}_now"] -= 1

        def fake_worker(clone, feature, note, **_kw):
            with lock:
                seq["n"] += 1
                mine = seq["n"]
            report = DevReport(feature=feature, succeeded=True, repo=str(clone))
            report.entries = {f"src/c{mine}.py": b"C = 1\n"}
            report.final_run_ref = f"run-{mine}"
            return report

        old = (P._run_worker, P._clone_workspace, P._clone_many, P._run_gate, P.run_review, P.sp)
        P._run_worker = fake_worker
        P._clone_workspace = lambda repo_root, overlay=None: self.repo
        P._clone_many = lambda repo_root, n: [self.repo] * n
        P._run_gate = lambda *a, **kw: track(
            "gates", gate_delay, lambda: GateResult(True, 0, "ok")
        )
        P.run_review = lambda *a, **kw: track(
            "reviews", review_delay,
            lambda: ReviewVerdict(approved=True, summary="ok", issues=[]),
        )

        class _FakeSp:
            @staticmethod
            def open(path):
                class _Ctx:
                    def __enter__(self):
                        return object()

                    def __exit__(self, *a):
                        return False

                return _Ctx()

        P.sp = _FakeSp()
        try:
            started = time.monotonic()
            report = P.develop_best_of(
                self.repo, "feat", k=k, test_cmd="true", provider="static",
                review_task=object(),
            )
            elapsed = time.monotonic() - started
        finally:
            (P._run_worker, P._clone_workspace, P._clone_many,
             P._run_gate, P.run_review, P.sp) = old
        return report, counters, elapsed

    def test_gates_never_overlap_but_reviews_do(self):
        _report, counters, _elapsed = self._run(k=3)
        self.assertEqual(counters["gates_max"], 1, "gates are CPU-bound: never two at once")
        self.assertGreater(counters["reviews_max"], 1, "reviews must overlap")

    def test_wall_clock_beats_the_serial_sum(self):
        k, review_delay, gate_delay = 3, 0.4, 0.2
        _report, _counters, elapsed = self._run(k, review_delay, gate_delay)
        serial = k * (review_delay + gate_delay)
        self.assertLess(elapsed, serial * 0.85, f"{elapsed:.2f}s vs serial {serial:.2f}s")

    def test_candidate_order_and_ranking_stay_deterministic(self):
        first, _c, _e = self._run(k=3)
        second, _c, _e = self._run(k=3)
        self.assertEqual([c.index for c in first.candidates], [0, 1, 2])
        self.assertEqual(
            [c.index for c in first.candidates], [c.index for c in second.candidates]
        )
        self.assertTrue(all(c.succeeded and c.gate_passed for c in first.candidates))


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class SharedGateStage(unittest.TestCase):
    """A3 + shared stage: run2 and best-of fell back to a FULL materialize on
    every gate. LocalGateStage already existed for the single-run path; here
    one pre-staged base serves the combined gate and its repairs (run2) and all
    K candidate gates (best-of), so the repo tree is copied once instead of
    once per gate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-stage-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("A = 1\n")

    def _count_full_copies(self, run):
        """Count tree copies whose SOURCE is the real repo — one per gate under
        the old path, one in total when a stage is shared."""
        from shepherd_dev import parallel as P
        from shepherd_dev import supervisor as S

        real = S.fast_copytree
        seen = {"n": 0}

        def counting(src, dest, ignored=None):
            if Path(src).resolve() == self.repo.resolve():
                seen["n"] += 1
            return real(src, dest, ignored)

        old = (P._run_worker, P._clone_workspace, P._clone_many, S.fast_copytree)
        S.fast_copytree = counting
        P._clone_workspace = lambda repo_root, overlay=None: self.repo
        P._clone_many = lambda repo_root, n: [self.repo] * n
        try:
            run(P)
        finally:
            (P._run_worker, P._clone_workspace, P._clone_many, S.fast_copytree) = old
        return seen["n"]

    def _worker(self, tag):
        from shepherd_dev.supervisor import DevReport

        import threading

        lock = threading.Lock()
        seq = {"n": 0}

        def fake_worker(clone, feature, note, **_kw):
            with lock:
                seq["n"] += 1
                mine = seq["n"]
            report = DevReport(feature=feature, succeeded=True, repo=str(clone))
            report.entries = {f"src/{tag}{mine}.py": b"X = 1\n"}
            report.final_run_ref = f"run-{mine}"
            return report

        return fake_worker

    def test_best_of_copies_the_repo_once_not_once_per_candidate(self):
        def run(P):
            P._run_worker = self._worker("c")
            P.develop_best_of(
                self.repo, "feat", k=3, test_cmd="true", provider="static",
            )

        self.assertEqual(
            self._count_full_copies(run), 1,
            "3 candidate gates must share one pre-staged base",
        )

    def test_run2_shares_one_base_across_the_combined_gate(self):
        def run(P):
            P._run_worker = self._worker("f")
            P.develop_parallel(
                self.repo, ["a", "b"], test_cmd="true", provider="static",
            )

        self.assertEqual(self._count_full_copies(run), 1)

    def test_stage_is_closed_even_when_the_gate_fails(self):
        from shepherd_dev import parallel as P

        closed = {"n": 0}

        def run(_P):
            P._run_worker = self._worker("c")
            real_start = P._start_local_gate_stage

            def tracking(repo_root, test_cmd):
                stage = real_start(repo_root, test_cmd)
                if stage is not None:
                    real_close = stage.close

                    def close():
                        closed["n"] += 1
                        return real_close()

                    stage.close = close
                return stage

            P._start_local_gate_stage = tracking
            try:
                P.develop_best_of(
                    self.repo, "feat", k=2, test_cmd="false", provider="static",
                )
            finally:
                P._start_local_gate_stage = real_start

        self._count_full_copies(run)
        self.assertEqual(closed["n"], 1, "the shared stage is closed exactly once")


if __name__ == "__main__":
    unittest.main()
