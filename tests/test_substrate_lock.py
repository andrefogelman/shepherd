"""Commands touching one repo's workspace store must not run concurrently.

_refresh_substrate rmtree's .vcscore and re-inits it. Anything else reading
that store at the same moment — a settle listing runs, another run deciding
whether it may reuse the adoption — can find the directory disappearing under
it, and two runs racing can rmtree each other's fresh adoption.

Runnable with: python -m unittest tests.test_substrate_lock
"""

from __future__ import annotations

import multiprocessing
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tmpdirs

from shepherd_dev.cli import (  # noqa: E402
    GITIGNORE_ENTRIES,
    _SUBSTRATE_LOCK_FILE,
    substrate_lock,
)


def _hold(repo: str, started, release, holder_ok):
    """Child process: take the lock, tell the parent, hold until released."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from shepherd_dev.cli import substrate_lock as sl

    with sl(Path(repo)) as got:
        holder_ok.value = 1 if got else 0
        started.set()
        release.wait(20)


class SubstrateLockExcludesConcurrentHolders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-lock-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)

    def test_a_second_process_waits_for_the_first(self):
        ctx = multiprocessing.get_context("spawn")
        started, release = ctx.Event(), ctx.Event()
        holder_ok = ctx.Value("i", 0)
        child = ctx.Process(
            target=_hold, args=(str(self.repo), started, release, holder_ok)
        )
        child.start()
        self.addCleanup(lambda: (release.set(), child.join(10)))
        self.assertTrue(started.wait(20), "the child never took the lock")
        self.assertEqual(holder_ok.value, 1, "flock unavailable in this environment")

        acquired = threading.Event()

        def _try():
            with substrate_lock(self.repo, timeout=20):
                acquired.set()

        waiter = threading.Thread(target=_try, daemon=True)
        waiter.start()
        # while the child holds it, nobody else gets in
        self.assertFalse(acquired.wait(1.0), "the lock did not exclude a second holder")

        release.set()
        child.join(10)
        self.assertTrue(acquired.wait(20), "the lock was never handed over")
        waiter.join(5)

    def test_the_lock_file_survives_a_vcscore_wipe(self):
        """It guards the rmtree of .vcscore, so it cannot live inside it."""
        import shutil

        vcscore = self.repo / ".vcscore"
        vcscore.mkdir()
        with substrate_lock(self.repo):
            shutil.rmtree(vcscore)
        self.assertTrue((self.repo / _SUBSTRATE_LOCK_FILE).exists())

    def test_reentrant_use_in_one_process_is_serialized_not_deadlocked(self):
        """Sequential acquisitions in the same process must not block."""
        started = time.monotonic()
        for _ in range(3):
            with substrate_lock(self.repo, timeout=5) as got:
                self.assertTrue(got)
        self.assertLess(time.monotonic() - started, 5)

    def test_an_unwritable_repo_degrades_instead_of_failing(self):
        missing = self.repo / "does-not-exist"
        with substrate_lock(missing) as got:
            self.assertFalse(got)  # yielded anyway: work is not blocked

    def test_the_lock_file_is_gitignored_by_init(self):
        self.assertIn(_SUBSTRATE_LOCK_FILE, GITIGNORE_ENTRIES)


try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class UnopenableStoreRecoversTests(unittest.TestCase):
    """A killed run can leave .vcscore in a state where opening it raises
    (LifecycleRecoveryRequiredError: "interrupted discard ... run
    recover_lifecycle first"). _refresh_substrate's rmtree IS the recovery,
    but it used to read the store first — to check for pending proposals —
    and reading is exactly what is broken, so the fix was unreachable and
    every later run in that repo failed. Every route the error message
    suggests dead-ends: `vcs-core` is not a real binary, and the API calls
    need an already-activated core.
    """

    def _repo(self):
        import subprocess

        from tmpdirs import mkdtemp

        repo = Path(mkdtemp(prefix="shepherd-brick-"))
        (repo / "a.py").write_text("A = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        # _adoption_key needs a HEAD to key on, so an empty repo has none
        subprocess.run(["git", "add", "a.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
            cwd=repo, check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli", "init", "--repo", str(repo)],
            input="", capture_output=True, text=True, check=True,
        )
        return repo

    def _break_store(self, repo: Path) -> None:
        for path in (repo / ".vcscore").rglob("*.json"):
            path.write_text("{ corrupted")

    def _opens(self, repo: Path) -> bool:
        import shepherd as sp

        try:
            with sp.open(repo):
                return True
        except Exception:
            return False

    def test_an_unopenable_store_is_replaced_rather_than_reported(self):
        from shepherd_dev.cli import _refresh_substrate

        repo = self._repo()
        self._break_store(repo)
        self.assertFalse(self._opens(repo), "precondition: the store must be broken")

        self.assertIsNone(_refresh_substrate(repo), "recovery must not report an error")
        self.assertTrue(self._opens(repo), "the store must be usable again")

    def test_a_matching_adoption_key_does_not_keep_a_broken_store(self):
        """The key shortcut skips the re-adopt when the worktree has not
        moved — which is only sound while the EXISTING store is good."""
        from shepherd_dev.cli import _ADOPT_KEY_FILE, _adoption_key, _refresh_substrate

        repo = self._repo()
        key = _adoption_key(repo)
        self.assertIsNotNone(key, "precondition: the repo must have an adoption key")
        (repo / ".vcscore" / _ADOPT_KEY_FILE).write_text(key, encoding="utf-8")
        self._break_store(repo)
        self.assertFalse(self._opens(repo))

        self.assertIsNone(_refresh_substrate(repo))
        self.assertTrue(self._opens(repo), "a matching key must not preserve a broken store")

    def test_a_readable_store_with_a_pending_proposal_is_still_refused(self):
        """The guard that protects unsettled work must survive the fix: only
        an UNREADABLE store skips the pending check."""
        from unittest.mock import patch

        from shepherd_dev.cli import _refresh_substrate

        repo = self._repo()

        class _Output:
            state = "unconsumed"

        class _Record:
            run_ref = "run-pending"

        class _Runs:
            def list(self):
                return [_Record()]

            def outputs(self, run_ref=None):
                return [_Output()]

        class _WS:
            runs = _Runs()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("shepherd_dev.cli.sp.open", return_value=_WS()):
            error = _refresh_substrate(repo)
        self.assertIsNotNone(error)
        self.assertIn("run-pending", error)


if __name__ == "__main__":
    unittest.main()
