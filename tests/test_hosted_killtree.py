"""A hosted worker that overruns its budget must not leave orphans.

The grok/codex executors ran their CLI through subprocess.run(timeout=...),
which on timeout kills only the DIRECT child. The CLI's own subprocesses (node,
MCP servers, tools it spawned) survived as orphans — still holding CPU, memory
and API sessions with nothing left to reap them.

The claude path solved this twice over: the launch perl puts the runner in its
own session and kills the process GROUP at the budget (#A), with the watchdog
as a second layer (#B). The hosted path had neither.

Runnable with: python -m unittest tests.test_hosted_killtree
"""

from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.providers.codex_exec import CliCodexExecutor  # noqa: E402
from shepherd_dev.providers.grok_exec import CliGrokExecutor  # noqa: E402


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class HostedWorkerTimeoutReapsTheTree(unittest.TestCase):
    """The stand-in CLI spawns a long-lived grandchild and reports its pid, the
    way a real agent CLI spawns node/MCP children."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-killtree-")
        self.addCleanup(self.tmp.cleanup)
        self.clone = Path(self.tmp.name)
        self.pidfile = self.clone / "grandchild.pid"
        # sh spawns a detached sleeper, writes its pid, then hangs past the budget
        self.fake_cli = self.clone / "fake-agent"
        self.fake_cli.write_text(
            "#!/bin/sh\n"
            "sleep 120 &\n"
            f'echo $! > "{self.pidfile}"\n'
            "sleep 120\n"
        )
        self.fake_cli.chmod(0o755)
        # The 30s floor protects a real CLI's startup; here it would just make
        # the suite wait, so shorten it for the duration of the test.
        from shepherd_dev.providers import hosted as H

        self._old_floor = H.MIN_WORKER_TIMEOUT
        H.MIN_WORKER_TIMEOUT = 1
        self.addCleanup(setattr, H, "MIN_WORKER_TIMEOUT", self._old_floor)

    def _grandchild_pid(self, deadline: float = 10.0) -> int:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            try:
                return int(self.pidfile.read_text().strip())
            except (OSError, ValueError):
                time.sleep(0.05)
        self.fail("the stand-in CLI never reported its grandchild")

    def _reap(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def _assert_tree_reaped(self, executor):
        result = executor.run(self.clone, "do a thing", budget_seconds=1)
        self.assertFalse(result.ok)
        self.assertIn("timed out", (result.error or ""))

        pid = self._grandchild_pid()
        self.addCleanup(self._reap, pid)
        # the group kill is synchronous with the timeout, but give the OS a beat
        for _ in range(40):
            if not _alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(
            _alive(pid),
            f"grandchild {pid} outlived the worker's budget — the CLI's own "
            f"subprocesses are orphaned, exactly what #A fixed for claude",
        )

    def test_grok_executor_reaps_the_whole_tree(self):
        self._assert_tree_reaped(CliGrokExecutor(grok_bin=str(self.fake_cli)))

    def test_codex_executor_reaps_the_whole_tree(self):
        self._assert_tree_reaped(CliCodexExecutor(codex_bin=str(self.fake_cli)))


class HostedWorkerNormalExit(unittest.TestCase):
    """The reaping must not disturb a worker that finishes on its own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-killtree-ok-")
        self.addCleanup(self.tmp.cleanup)
        self.clone = Path(self.tmp.name)

    def _cli(self, body: str) -> str:
        path = self.clone / "fake-agent"
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return str(path)

    def test_success_keeps_output_and_exit_code(self):
        ex = CliGrokExecutor(grok_bin=self._cli("echo hello; echo warn >&2; exit 0"))
        result = ex.run(self.clone, "p", budget_seconds=30)
        self.assertTrue(result.ok, result.error)
        self.assertIn("hello", result.output_tail)
        self.assertIn("warn", result.output_tail)  # stderr still captured

    def test_nonzero_exit_is_reported_not_swallowed(self):
        ex = CliCodexExecutor(codex_bin=self._cli("echo boom; exit 3"))
        result = ex.run(self.clone, "p", budget_seconds=30)
        self.assertFalse(result.ok)
        self.assertIn("3", result.error or "")
        self.assertIn("boom", result.output_tail)


if __name__ == "__main__":
    unittest.main()
