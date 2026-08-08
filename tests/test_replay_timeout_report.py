"""A replay timeout must name the step that actually timed out.

Seen while diagnosing an unrelated failure: the suite printed

    replay timed out after 1020s ('f') — scored as a failure, but this is a
    harness limit, not a candidate verdict

while the step that had actually expired was `shepherd-dev init`, whose
timeout is 120s. One `except subprocess.TimeoutExpired` covers both
subprocess calls, and the message hardcoded the OTHER one's budget.

The two point at different problems: a `run` timeout means the worker or the
gate needed more than its budget, and a bigger budget might fix it. An `init`
timeout means the workspace could not be created in two minutes — a stuck
substrate or a prompt waiting on stdin — and no budget fixes that. Reporting
the first when it was the second sends the reader looking in the wrong place.

Runnable with: python -m unittest tests.test_replay_timeout_report
"""

from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class _Ok:
    returncode = 0


class TimeoutIsReportedHonestlyTests(unittest.TestCase):
    def _replay_with(self, raiser):
        """Drive _replay with a fake subprocess.run and capture stderr."""
        from tmpdirs import mkdtemp

        from shepherd_dev import optimize as O
        from shepherd_dev.optimize import ReplayCase

        repo = Path(mkdtemp(prefix="shepherd-replay-"))
        real = subprocess.run
        real(["git", "init", "-q"], cwd=repo)
        (repo / "a.txt").write_text("a\n")
        real(["git", "add", "-A"], cwd=repo)
        real(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"], cwd=repo)
        sha = real(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()

        def fake_run(argv, **kw):
            if isinstance(argv, list) and "shepherd_dev.cli" in argv:
                step = "init" if "init" in argv else "run"
                out = raiser(step, kw.get("timeout"))
                if out is not None:
                    return out
            return real(argv, **kw)

        buf = io.StringIO()
        O.subprocess.run = fake_run
        try:
            with redirect_stderr(buf):
                result = O._replay(
                    ReplayCase(repo=str(repo), sha=sha, feature="add a thing",
                               test_cmd="true", mode="feature", was_success=True),
                    None, worker_budget=120,
                )
        finally:
            O.subprocess.run = real
        return result, buf.getvalue()

    def test_a_run_timeout_names_the_run_and_its_own_budget(self):
        from shepherd_dev.optimize import _replay_timeout

        def _raise(step, timeout):
            if step == "run":
                raise subprocess.TimeoutExpired(cmd="run", timeout=timeout)
            return _Ok()

        result, err = self._replay_with(_raise)
        self.assertFalse(result)
        self.assertIn(str(_replay_timeout(120)), err)
        self.assertIn("run", err.lower())

    def test_an_init_timeout_names_init_and_ITS_budget(self):
        """The seed defect: this used to print the run's 1020s."""
        from shepherd_dev.optimize import INIT_TIMEOUT, _replay_timeout

        def _raise(step, timeout):
            if step == "init":
                raise subprocess.TimeoutExpired(cmd="init", timeout=timeout)
            return _Ok()

        result, err = self._replay_with(_raise)
        self.assertFalse(result)
        self.assertIn(str(INIT_TIMEOUT), err)
        self.assertIn("init", err.lower())
        self.assertNotIn(
            str(_replay_timeout(120)), err,
            "reporting the run's budget for an init timeout is the defect",
        )

    def test_the_message_still_says_it_is_a_harness_limit_not_a_verdict(self):
        """Whichever step expired, a reaped replay is not evidence against the
        candidate prompt — folding it silently into 'failed' biases optimize
        toward rejecting good prompts."""
        def _raise(step, timeout):
            if step == "init":
                raise subprocess.TimeoutExpired(cmd="init", timeout=timeout)
            return _Ok()

        _, err = self._replay_with(_raise)
        self.assertIn("harness limit", err)
        self.assertIn("not a candidate verdict", err)

    def test_the_feature_is_still_named(self):
        def _raise(step, timeout):
            if step == "run":
                raise subprocess.TimeoutExpired(cmd="run", timeout=timeout)
            return _Ok()

        _, err = self._replay_with(_raise)
        self.assertIn("add a thing", err)


class InitTimeoutIsNamedTests(unittest.TestCase):
    def test_the_constant_exists_and_is_the_value_in_use(self):
        """It was an inline 120 — invisible to the message that had to quote
        it, and to anyone tuning it."""
        import inspect

        from shepherd_dev import optimize as O

        self.assertIsInstance(O.INIT_TIMEOUT, int)
        src = inspect.getsource(O._replay)
        self.assertIn("timeout=INIT_TIMEOUT", src)


if __name__ == "__main__":
    unittest.main()
