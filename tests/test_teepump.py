"""Tests for the worker tee-pump seam (Fase 2 of verbose mode): the perl
killtree+pump script, the argv swap, the stream hook, and the execution proxy
that drains the tailer before the provider scrubs the scratch.
Runnable with: python -m unittest tests.test_teepump
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.events import RunEventLog, WorkerStreamHook  # noqa: E402
from shepherd_dev.supervisor import (  # noqa: E402
    _TEEPUMP_PERL,
    _TailingExecution,
    _swap_perl_teepump,
)

PERL = "/usr/bin/perl"


class SwapArgvTests(unittest.TestCase):
    BASE = [
        "/usr/bin/perl", "-e", "alarm shift @ARGV; exec @ARGV or die qq{exec: $!}",
        "900", "/usr/bin/env", "HOME=/x", "claude", "-p", "prompt",
    ]

    def test_swaps_script_and_inserts_tee_after_budget(self):
        argv = _swap_perl_teepump(list(self.BASE), "/ws/.claude-scratch/tmp/worker-stream.ndjson")
        self.assertEqual(argv[2], _TEEPUMP_PERL)
        self.assertEqual(argv[3], "900")
        self.assertEqual(argv[4], "/ws/.claude-scratch/tmp/worker-stream.ndjson")
        self.assertEqual(argv[5:], self.BASE[4:])

    def test_non_perl_argv_is_untouched(self):
        argv = ["/bin/sh", "-c", "echo hi"]
        self.assertEqual(_swap_perl_teepump(list(argv), "/tmp/t"), argv)

    def test_watchdog_marker_preserved(self):
        # The watchdog finds worker processes by this marker (worker_watchdog._WORKER_MARKERS).
        self.assertIn("exec @ARGV", _TEEPUMP_PERL)


@unittest.skipUnless(os.path.exists(PERL), "perl not available")
class PumpBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-pump-")
        self.addCleanup(self.tmp.cleanup)
        self.tee = Path(self.tmp.name) / "tee.ndjson"

    def _run(self, budget: str, body: str, timeout: int = 30):
        argv = [PERL, "-e", _TEEPUMP_PERL, budget, str(self.tee), "/bin/sh", "-c", body]
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    def test_stdout_exit_code_and_tee_are_faithful(self):
        proc = self._run("10", 'printf \'{"a":1}\\n{"b":2}\\n\'; exit 7')
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(proc.stdout, '{"a":1}\n{"b":2}\n')
        self.assertEqual(self.tee.read_text(encoding="utf-8"), '{"a":1}\n{"b":2}\n')

    def test_stderr_passes_through(self):
        proc = self._run("10", 'echo out; echo err >&2')
        self.assertIn("out", proc.stdout)
        self.assertIn("err", proc.stderr)
        self.assertNotIn("err", self.tee.read_text(encoding="utf-8"))

    def test_budget_kills_group_with_rc_124_and_tee_flushed(self):
        start = time.monotonic()
        proc = self._run("1", 'echo started; sleep 30; echo never')
        elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, 124)
        self.assertLess(elapsed, 10)
        self.assertIn("started", self.tee.read_text(encoding="utf-8"))
        self.assertNotIn("never", proc.stdout)


class _StubExecution:
    """Stands in for the substrate ExecutionCapability: launch writes the tee
    file (as the jailed pump would) and then 'the provider' scrubs it right
    after launch returns — the race the proxy must win."""

    def __init__(self, working_path: Path, lines: str):
        self.working_path = working_path
        self.identity = "stub-identity"
        self._lines = lines
        self.scrubbed = False

    def launch_confined(self, command, confinement):
        tee = self.working_path / ".claude-scratch" / "tmp" / "worker-stream.ndjson"
        tee.parent.mkdir(parents=True, exist_ok=True)
        tee.write_text(self._lines, encoding="utf-8")
        time.sleep(0.05)  # let the tailer observe mid-run at least once
        return "completed-proc"


class TailingExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-texec-")
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.log = RunEventLog(run_id="texec", root=self.ws / "runs")

    def test_events_survive_the_post_launch_scrub(self):
        line = (
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
            '"name":"Edit","input":{"file_path":"a.py","old_string":"1\\n","new_string":"2\\n"}}]}}\n'
        )
        hook = WorkerStreamHook(self.log)
        hook.attempt = 2
        inner = _StubExecution(self.ws, line)
        proxy = _TailingExecution(inner, hook)
        result = proxy.launch_confined(["cmd"], "confinement")
        # Simulate the provider's finally-scrub AFTER launch_confined returned:
        tee = self.ws / ".claude-scratch" / "tmp" / "worker-stream.ndjson"
        tee.unlink()
        self.assertEqual(result, "completed-proc")
        events = [
            e for e in map(__import__("json").loads, self.log.path.read_text().splitlines())
        ]
        kinds = [e["kind"] for e in events]
        self.assertIn("worker.tool", kinds)
        self.assertIn("worker.edit", kinds)
        self.assertTrue(all(e.get("attempt") == 2 for e in events))

    def test_attribute_passthrough(self):
        proxy = _TailingExecution(_StubExecution(self.ws, ""), WorkerStreamHook(self.log))
        self.assertEqual(proxy.identity, "stub-identity")
        self.assertEqual(proxy.working_path, self.ws)

    def test_hook_failure_does_not_block_launch(self):
        class _BoomHook:
            def start(self, working_path):
                raise RuntimeError("tailer boom")

            def drain(self, tailer, timeout=2.0):  # pragma: no cover — never reached
                raise AssertionError("drain on failed start")

        inner = _StubExecution(self.ws, "")
        proxy = _TailingExecution(inner, _BoomHook())
        self.assertEqual(proxy.launch_confined(["cmd"], "c"), "completed-proc")


class WorkerStreamHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-hook-")
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.log = RunEventLog(run_id="hook", root=self.ws / "runs")

    def test_tee_path_layout(self):
        hook = WorkerStreamHook(self.log)
        self.assertEqual(
            hook.tee_path(self.ws),
            self.ws / ".claude-scratch" / "tmp" / "worker-stream.ndjson",
        )

    def test_start_and_drain_round_trip(self):
        hook = WorkerStreamHook(self.log)
        tailer = hook.start(self.ws)
        tee = hook.tee_path(self.ws)
        tee.parent.mkdir(parents=True, exist_ok=True)
        tee.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n')
        hook.drain(tailer)
        kinds = [e["kind"] for e in map(__import__("json").loads, self.log.path.read_text().splitlines())]
        self.assertIn("worker.note", kinds)


if __name__ == "__main__":
    unittest.main()
