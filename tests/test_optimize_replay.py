"""#7: the CRO replay subprocess must be allowed to finish.

_replay shells out to `shepherd-dev run`, which spends worker_budget on the
worker AND up to a full gate timeout on the suite afterwards. The parent
timeout only allowed worker_budget + 300, so a replay was killed partway
through its gate and the candidate it was validating was scored as a failure.

Runnable with: python -m unittest tests.test_optimize_replay
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev.optimize import (  # noqa: E402
    REPLAY_GATE_TIMEOUT,
    _replay_timeout,
)


class ReplayTimeout(unittest.TestCase):
    def test_covers_the_worker_and_the_whole_gate(self):
        for budget in (60, 600, 1800):
            with self.subTest(budget=budget):
                self.assertGreater(
                    _replay_timeout(budget),
                    budget + REPLAY_GATE_TIMEOUT,
                    "a replay must outlive its worker plus a full gate",
                )

    def test_old_budget_was_short_by_at_least_a_gate(self):
        # Pins the size of the defect: the previous worker_budget + 300 could
        # not even cover the gate's default 600s.
        self.assertGreater(_replay_timeout(600), 600 + 300)


class ReplayInvocation(unittest.TestCase):
    def test_run_is_invoked_with_an_explicit_gate_timeout(self):
        """The parent's budget is only sound if the child's gate is pinned to
        the value it was computed from — inheriting the CLI default would let
        the two drift apart."""
        import subprocess
        import tempfile

        from shepherd_dev import optimize as O
        from shepherd_dev.optimize import ReplayCase

        seen: dict = {}
        real_run = subprocess.run

        def fake_run(argv, **kw):
            if isinstance(argv, list) and "run" in argv and "--test-cmd" in argv:
                seen["argv"] = list(argv)
                seen["timeout"] = kw.get("timeout")

                class _P:
                    returncode = 0

                return _P()
            return real_run(argv, **kw)

        repo = Path(mkdtemp())
        real_run(["git", "init", "-q"], cwd=repo)
        real_run(["git", "config", "user.email", "t@t"], cwd=repo)
        real_run(["git", "config", "user.name", "t"], cwd=repo)
        (repo / "a.txt").write_text("a\n")
        real_run(["git", "add", "-A"], cwd=repo)
        real_run(["git", "commit", "-qm", "c"], cwd=repo)
        sha = real_run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()

        O.subprocess.run = fake_run
        try:
            O._replay(
                ReplayCase(
                    repo=str(repo), sha=sha, feature="f", test_cmd="true",
                    mode="feature", was_success=True,
                ),
                None,
                worker_budget=120,
            )
        finally:
            O.subprocess.run = real_run

        self.assertIn("--gate-timeout", seen.get("argv", []))
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--gate-timeout") + 1], str(REPLAY_GATE_TIMEOUT))
        self.assertEqual(seen["timeout"], _replay_timeout(120))


if __name__ == "__main__":
    unittest.main()
