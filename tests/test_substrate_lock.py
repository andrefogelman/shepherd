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


if __name__ == "__main__":
    unittest.main()
