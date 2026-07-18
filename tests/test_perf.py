"""Tests for the execution-speed work: parallel clone creation, the fast
copy helper, the pre-staged local gate, the adoption cache key, and the
speculative review overlap. Runnable with: python -m unittest tests.test_perf
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


try:  # parallel.py imports the substrate; skip where absent
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class ParallelClonesTests(unittest.TestCase):
    def test_clone_many_runs_concurrently_and_keeps_order(self):
        from shepherd_dev import parallel as P

        calls: list[int] = []

        def slow_clone(repo_root, overlay=None):
            import uuid

            calls.append(1)
            time.sleep(0.2)
            return Path(f"/fake/clone-{uuid.uuid4().hex}")

        old = P._clone_workspace
        P._clone_workspace = slow_clone
        try:
            t0 = time.monotonic()
            clones = P._clone_many(Path("/fake/repo"), 3)
            elapsed = time.monotonic() - t0
        finally:
            P._clone_workspace = old
        self.assertEqual(len(clones), 3)
        self.assertLess(elapsed, 0.5)  # serial would be ≥0.6s
        self.assertEqual(len(set(map(str, clones))), 3)


class FastCopytreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-fct-")
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "src"
        (self.src / "pkg").mkdir(parents=True)
        (self.src / "pkg" / "a.py").write_text("A = 1\n")
        (self.src / "top.txt").write_text("top\n")
        (self.src / ".git").mkdir()
        (self.src / ".git" / "config").write_text("x\n")
        (self.src / "node_modules").mkdir()
        (self.src / "node_modules" / "big.js").write_text("junk\n")

    def test_copies_tree_and_skips_ignored_top_level(self):
        from shepherd_dev.supervisor import fast_copytree

        dst = Path(self.tmp.name) / "dst"
        fast_copytree(self.src, dst, ignored={".git", "node_modules"})
        self.assertEqual((dst / "pkg" / "a.py").read_text(), "A = 1\n")
        self.assertEqual((dst / "top.txt").read_text(), "top\n")
        self.assertFalse((dst / ".git").exists())
        self.assertFalse((dst / "node_modules").exists())

    def test_dest_may_exist(self):
        from shepherd_dev.supervisor import fast_copytree

        dst = Path(self.tmp.name) / "dst"
        dst.mkdir()
        fast_copytree(self.src, dst, ignored=set())
        self.assertTrue((dst / "pkg" / "a.py").is_file())


class LocalGateStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-stage-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.py").write_text("V = 1\n")

    def test_stage_overlays_entries_on_a_pristine_base(self):
        from shepherd_dev.supervisor import LocalGateStage

        stage = LocalGateStage(self.repo).start()
        try:
            work1 = stage.stage({"src/a.py": b"V = 2\n", "src/new.py": b"N = 1\n"})
            self.assertEqual((work1 / "src" / "a.py").read_text(), "V = 2\n")
            self.assertEqual((work1 / "src" / "new.py").read_text(), "N = 1\n")
            # second attempt: pristine again (no leak from attempt 1)
            work2 = stage.stage({"src/a.py": b"V = 3\n"})
            self.assertEqual((work2 / "src" / "a.py").read_text(), "V = 3\n")
            self.assertFalse((work2 / "src" / "new.py").exists())
        finally:
            stage.close()

    def test_gate_uses_the_stage_and_still_judges(self):
        from shepherd_dev.supervisor import LocalGateStage, _run_gate

        stage = LocalGateStage(self.repo).start()
        try:
            res = _run_gate(
                self.repo,
                {"src/a.py": b"V = 2\n"},
                'python3 -c "import sys; sys.path.insert(0, \'src\'); import a; sys.exit(0 if a.V == 2 else 1)"',
                timeout=60,
                warmup=stage,
            )
        finally:
            stage.close()
        self.assertTrue(res.passed, res.output_tail)


class AdoptionKeyTests(unittest.TestCase):
    def setUp(self):
        import subprocess

        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-adopt-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "a.py").write_text("A = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
            cwd=self.repo, check=True,
        )

    def _key(self):
        from shepherd_dev.cli import _adoption_key

        return _adoption_key(self.repo)

    def test_stable_when_nothing_changes(self):
        self.assertEqual(self._key(), self._key())
        self.assertIsNotNone(self._key())

    def test_changes_on_edit_untracked_and_commit(self):
        import subprocess

        k0 = self._key()
        (self.repo / "a.py").write_text("A = 2\n")  # dirty tracked file
        k1 = self._key()
        self.assertNotEqual(k0, k1)
        (self.repo / "new.py").write_text("N = 1\n")  # untracked file
        k2 = self._key()
        self.assertNotEqual(k1, k2)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c2"],
            cwd=self.repo, check=True,
        )
        self.assertNotEqual(k2, self._key())

    def test_non_git_dir_returns_none(self):
        from shepherd_dev.cli import _adoption_key

        with tempfile.TemporaryDirectory() as plain:
            self.assertIsNone(_adoption_key(Path(plain)))  # no cache without git


class SpeculativeReviewTests(unittest.TestCase):
    """develop() overlaps the reviewer with the gate when speculative_review
    is on: the reviewer starts BEFORE the gate finishes; its verdict is used
    on gate pass and discarded on gate fail."""

    def _develop(self, test_cmd: str, monkey_review):
        from shepherd_dev import supervisor as S

        repo = Path(tempfile.mkdtemp(prefix="shepherd-spec-"))
        (repo / "seed.txt").write_text("s\n")

        class _CS:
            def __init__(self, files):
                self._files = files

            @property
            def changed_paths(self):
                return list(self._files)

            def read_file(self, rel):
                b = self._files.get(rel)
                return (b, 0o644) if b is not None else None

        class _Out:
            def __init__(self, cs):
                self._cs = cs

            def changeset(self):
                return self._cs

            def discard(self):
                pass

        class _Run:
            def __init__(self):
                self.run_ref = "r1"
                self._o = _Out(_CS({"impl.py": b"X = 1\n"}))

            def output(self):
                return self._o

        class _Tasks:
            def register(self, task):
                pass

        class _WS:
            tasks = _Tasks()

            def run(self, task, **kw):
                return _Run()

            def git_repo(self):
                return None

        old = S.run_review
        S.run_review = monkey_review
        try:
            return S.develop(
                _WS(), None, repo=None, repo_root=repo, feature="f",
                test_cmd=test_cmd, provider="static", placement="advisory",
                max_attempts=1, review_task=object(), speculative_review=True,
            )
        finally:
            S.run_review = old

    def test_review_overlaps_gate_and_is_used_on_pass(self):
        from shepherd_dev.supervisor import ReviewVerdict

        started = []

        def fake_review(*a, **kw):
            started.append(time.monotonic())
            return ReviewVerdict(approved=True, summary="ok", issues=[])

        t0 = time.monotonic()
        report = self._develop("sleep 1; echo ok", fake_review)
        self.assertTrue(report.succeeded)
        self.assertIsNotNone(report.review)
        self.assertTrue(report.review.approved)
        # reviewer started while the 1s gate was still sleeping
        self.assertLess(started[0] - t0, 0.9)

    def test_review_discarded_on_gate_fail(self):
        from shepherd_dev.supervisor import ReviewVerdict

        def fake_review(*a, **kw):
            return ReviewVerdict(approved=True, summary="ok", issues=[])

        report = self._develop("exit 1", fake_review)
        self.assertFalse(report.succeeded)
        self.assertIsNone(report.review)  # speculative result thrown away


if __name__ == "__main__":
    unittest.main()
