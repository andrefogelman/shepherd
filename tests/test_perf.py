"""Tests for the execution-speed work: parallel clone creation, the fast
copy helper, the pre-staged local gate, the adoption cache key, and the
speculative review overlap. Runnable with: python -m unittest tests.test_perf
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402


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

    def test_skips_nested_git_under_a_copied_subtree(self):
        """A git-sourced dependency (Elixir's `mix.exs` {:git, ...} deps are
        the common case) plants its own .git several levels under a
        directory that isn't itself ignored — `cp -R` copies that whole
        subtree in one shot, so the top-level-only filter used to miss it."""
        from shepherd_dev.supervisor import fast_copytree

        deps_git = self.src / "deps" / "some_pkg" / ".git"
        deps_git.mkdir(parents=True)
        (deps_git / "config").write_text("nested\n")
        (self.src / "deps" / "some_pkg" / "mix.exs").write_text("defmodule; end\n")

        dst = Path(self.tmp.name) / "dst"
        fast_copytree(self.src, dst, ignored={".git", "node_modules"})

        self.assertFalse((dst / ".git").exists())
        self.assertFalse((dst / "deps" / "some_pkg" / ".git").exists())
        self.assertEqual(
            (dst / "deps" / "some_pkg" / "mix.exs").read_text(), "defmodule; end\n"
        )


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

    def test_stage_never_carries_the_repos_own_git_dir(self):
        """The real repo's root .git must not ride into the stage: a tree
        containing a literally-named `.git` entry is exactly what breaks the
        substrate's vcs_core when it later builds a tree object from it."""
        from shepherd_dev.supervisor import LocalGateStage

        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "config").write_text("x\n")

        stage = LocalGateStage(self.repo).start()
        try:
            work = stage.stage({})  # blocks until the base build finishes
            assert stage.base is not None
            assert work is not None
            self.assertFalse((stage.base / ".git").exists())
            self.assertFalse((work / ".git").exists())
        finally:
            stage.close()

    def test_a_speculative_stage_is_torn_down_when_the_gate_turns_out_remote(self):
        """start_local_gate_stage never hands out a SHARED stage for a
        remote-gated repo, so any LocalGateStage reaching _run_gate's remote
        branch is the single-use, speculative kind — leaving it unclosed
        leaks its base/.git-bearing tree and background thread forever."""
        from shepherd_dev import config as _config
        from shepherd_dev import remotegate as RG
        from shepherd_dev.remotegate import parse_remote_config
        from shepherd_dev.supervisor import GateResult, LocalGateStage, _run_gate

        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "config").write_text("x\n")

        stage = LocalGateStage(self.repo).start()
        stage.stage({})  # blocks until the base build finishes
        stage_root = stage._root
        self.assertTrue(stage_root.exists())

        cfg = parse_remote_config({
            "ssh": "root@host",
            "repo_dir": str(self.tmp.name),
            "copy_cmd": "true",
            "test_cmd": "true",
            "workdir_base": str(self.tmp.name),
        }, "python")
        self.assertIsNotNone(cfg)
        fake_result = GateResult(passed=True, exit_code=0, output_tail="")

        old_remote_gate, old_run_remote_gate = _config.remote_gate, RG.run_remote_gate
        _config.remote_gate = lambda repo_root: cfg
        RG.run_remote_gate = lambda *a, **k: fake_result
        try:
            res = _run_gate(self.repo, {}, "true", timeout=5, warmup=stage)
        finally:
            _config.remote_gate = old_remote_gate
            RG.run_remote_gate = old_run_remote_gate

        self.assertIs(res, fake_result)
        self.assertFalse(stage_root.exists())


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

    def test_edit_inside_an_untracked_directory_moves_the_key(self):
        """#5: `git status --porcelain` collapses an untracked directory to one
        `?? dir/` entry, and stat() of a DIRECTORY does not move when a file
        inside it is rewritten in place. The fingerprint therefore matched
        across a real edit and the worker built on a stale base."""
        pkg = self.repo / "untracked_pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text("M = 1\n")
        k0 = self._key()
        (pkg / "mod.py").write_text("M = 2\n")  # in-place edit, dir mtime unmoved
        self.assertNotEqual(k0, self._key())

    def test_a_same_size_rewrite_on_the_same_mtime_moves_the_key(self):
        """The key decides whether the worker may build on the CACHED adoption.
        Keying a dirty file on (mtime, size) is the evidence the diff fast-path
        was removed for: on a coarse-timestamp filesystem a same-size rewrite
        can land on the recorded mtime, and the stale adoption is then reused
        with nothing said. Forced with utime, so it is not a race with the
        clock and holds on every filesystem."""
        target = self.repo / "a.py"
        target.write_text("A = 2\n")  # dirty, so it appears in git status
        st = target.stat()
        k0 = self._key()

        target.write_text("A = 3\n")  # same size, different content
        os.utime(target, ns=(st.st_mtime_ns, st.st_mtime_ns))
        self.assertEqual(target.stat().st_size, st.st_size)
        self.assertEqual(target.stat().st_mtime_ns, st.st_mtime_ns)

        self.assertNotEqual(k0, self._key())

    def test_same_content_touched_to_a_new_mtime_keeps_the_key(self):
        """The converse: content is what the adoption depends on, so a bare
        touch must NOT force a multi-second re-adoption."""
        target = self.repo / "a.py"
        target.write_text("A = 2\n")
        k0 = self._key()
        os.utime(target, ns=(0, 0))
        self.assertEqual(k0, self._key())

    def test_a_retargeted_symlink_moves_the_key(self):
        """A dirty path need not be a regular file. The symlink branch was a
        latent NameError before `os` was imported here, and nothing reached it."""
        (self.repo / "target_a.py").write_text("A\n")
        (self.repo / "target_b.py").write_text("B\n")
        link = self.repo / "link.py"
        link.symlink_to("target_a.py")
        k0 = self._key()
        link.unlink()
        link.symlink_to("target_b.py")
        self.assertNotEqual(k0, self._key())

    def test_an_unreadable_dirty_file_does_not_freeze_the_key(self):
        target = self.repo / "secret.py"
        target.write_text("S = 1\n")
        self.assertIsNotNone(self._key())
        target.chmod(0o000)
        self.addCleanup(target.chmod, 0o644)
        if os.access(target, os.R_OK):
            self.skipTest("running as root: permissions do not bite")
        # unreadable is not evidence of unchanged — the key must still resolve
        self.assertIsNotNone(self._key())

    def test_a_huge_dirty_file_is_streamed_not_slurped(self):
        big = self.repo / "big.bin"
        big.write_bytes(b"\0" * (3 * (1 << 20)))  # 3 chunks
        k0 = self._key()
        with big.open("r+b") as fh:  # flip one byte in the last chunk
            fh.seek(3 * (1 << 20) - 1)
            fh.write(b"\1")
        self.assertNotEqual(k0, self._key())

    def test_a_dirty_file_that_disappears_moves_the_key(self):
        target = self.repo / "a.py"
        target.write_text("A = 2\n")
        k0 = self._key()
        target.unlink()
        self.assertNotEqual(k0, self._key())

    def test_new_file_in_an_untracked_directory_moves_the_key(self):
        pkg = self.repo / "untracked_pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text("M = 1\n")
        k0 = self._key()
        (pkg / "other.py").write_text("O = 1\n")
        self.assertNotEqual(k0, self._key())


class StartupOverlapTests(unittest.TestCase):
    """A6: the context pack and the gate resolution (whose remote branch
    ssh-preflights the host) share nothing, but ran end to end."""

    def setUp(self):
        self.repo = Path(mkdtemp(prefix="shepherd-startup-"))
        (self.repo / "a.py").write_text("A = 1\n")

    def _args(self):
        import argparse

        return argparse.Namespace(
            feature="a feature", repo=str(self.repo), test_cmd=None,
            provider="static", allowed_prefix=[], no_context_pack=False,
            no_plan=False, best_of=1, no_review=True, auto_settle=False,
            review_rounds=1, review_panel=None, mode="feature", quiet=True, verbose=False,
            json=False, fresh_adopt=False,
        )

    def _run(self, delay=0.4):
        import time

        from shepherd_dev import cli as C

        marks: dict = {}

        def slow_pack(args, repo_root, feature_text, scan=None, out=None):
            marks["pack_start"] = time.monotonic()
            time.sleep(delay)
            marks["pack_end"] = time.monotonic()
            return "PACK", {"planned_files": []}

        def slow_gate(repo_root, cmd, provider):
            marks["gate_start"] = time.monotonic()
            time.sleep(delay)
            marks["gate_end"] = time.monotonic()
            return "true", None, True

        class _Stop(Exception):
            pass

        def stop_here(repo_root, fresh=False):
            raise _Stop  # everything under test has already happened

        old = (C._build_pack, C._resolve_gate, C._resolve_repo, C._refresh_substrate)
        C._build_pack = slow_pack
        C._resolve_gate = slow_gate
        C._resolve_repo = lambda repo: self.repo
        C._refresh_substrate = stop_here
        started = time.monotonic()
        try:
            C.cmd_run(self._args())
        except _Stop:
            pass
        finally:
            (C._build_pack, C._resolve_gate, C._resolve_repo,
             C._refresh_substrate) = old
        return marks, time.monotonic() - started

    def test_pack_and_gate_resolution_overlap(self):
        marks, _elapsed = self._run()
        self.assertLess(marks["pack_start"], marks["gate_end"])
        self.assertLess(marks["gate_start"], marks["pack_end"])

    def test_startup_is_not_the_serial_sum(self):
        delay = 0.4
        _marks, elapsed = self._run(delay)
        self.assertLess(elapsed, 2 * delay * 0.85, f"{elapsed:.2f}s vs {2 * delay:.2f}s serial")

    def test_the_pack_thread_never_outlives_the_startup(self):
        self._run(0.1)
        self.assertFalse(
            any(t.name == "shepherd-pack" and t.is_alive() for t in threading.enumerate())
        )


class SpeculativeReviewTests(unittest.TestCase):
    """develop() overlaps the reviewer with the gate when speculative_review
    is on: the reviewer starts BEFORE the gate finishes; its verdict is used
    on gate pass and discarded on gate fail."""

    def _develop(self, test_cmd: str, monkey_review, on_discard=None):
        from shepherd_dev import supervisor as S

        repo = Path(mkdtemp(prefix="shepherd-spec-"))
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
                if on_discard is not None:
                    on_discard()

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

    def test_reviewer_is_reaped_before_the_gate_fail_path_proceeds(self):
        """The spec thread holds the workspace and reads the very changeset the
        failure path discards; leaving it running let it overlap the NEXT
        attempt's workspace.run. join() only existed on the review path, which
        both gate-failure exits jump over."""
        from shepherd_dev.supervisor import ReviewVerdict

        def spec_alive():
            return any(
                t.name == "shepherd-spec-review" and t.is_alive()
                for t in threading.enumerate()
            )

        alive_at_discard = []
        release = threading.Event()

        def slow_review(*a, **kw):
            release.wait(10)  # outlives the (instantly failing) gate
            return ReviewVerdict(approved=True, summary="ok", issues=[])

        def on_discard():
            release.set()  # let the reviewer finish, then observe
            alive_at_discard.append(spec_alive())

        report = self._develop("exit 1", slow_review, on_discard=on_discard)
        self.assertFalse(report.succeeded)
        self.assertEqual(
            alive_at_discard, [False],
            "the spec reviewer must be joined before output.discard() runs",
        )
        self.assertFalse(spec_alive(), "no spec reviewer may outlive the attempt")


if __name__ == "__main__":
    unittest.main()
