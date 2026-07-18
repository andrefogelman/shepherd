"""Tests for gate output streaming (Fase 3 of verbose mode): the line-streamed
subprocess runner with process-group kill, the gate line observer, and the
local + remote gates emitting per-line / per-failure events.
Runnable with: python -m unittest tests.test_gatestream
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev import procstream as PS  # noqa: E402
from shepherd_dev import remotegate as RG  # noqa: E402
from shepherd_dev.events import RunEventLog, gate_line_observer  # noqa: E402
from shepherd_dev.procstream import run_streaming  # noqa: E402
from shepherd_dev.remotegate import parse_remote_config, run_remote_gate  # noqa: E402
from shepherd_dev.supervisor import _run_gate  # noqa: E402


class RunStreamingTests(unittest.TestCase):
    def test_output_exit_code_and_merged_streams(self):
        seen: list[str] = []
        res = run_streaming(
            ["sh", "-c", "echo one; echo two >&2; exit 3"], on_line=seen.append
        )
        self.assertEqual(res.returncode, 3)
        self.assertFalse(res.timed_out)
        self.assertIn("one", res.output)
        self.assertIn("two", res.output)  # stderr merged, chronological
        self.assertIn("one", seen)
        self.assertIn("two", seen)
        self.assertTrue(all(not s.endswith("\n") for s in seen))

    def test_shell_mode(self):
        res = run_streaming("echo shellmode", shell=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("shellmode", res.output)

    def test_timeout_kills_the_process_group(self):
        start = time.monotonic()
        res = run_streaming(["sh", "-c", "echo alive; sleep 30; echo never"], timeout=1)
        elapsed = time.monotonic() - start
        self.assertTrue(res.timed_out)
        self.assertLess(elapsed, 10)
        self.assertIn("alive", res.output)
        self.assertNotIn("never", res.output)

    def test_on_line_errors_are_swallowed(self):
        def boom(_line):
            raise RuntimeError("observer bug")

        res = run_streaming(["sh", "-c", "echo a; echo b"], on_line=boom)
        self.assertEqual(res.returncode, 0)
        self.assertIn("a", res.output)


class GateLineObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-glo-")
        self.addCleanup(self.tmp.cleanup)
        self.log = RunEventLog(run_id="glo", root=Path(self.tmp.name))
        self.seen: list[dict] = []
        self.log.subscribe(self.seen.append)

    def kinds(self):
        return [e["kind"] for e in self.seen]

    def test_failure_line_emits_line_and_named_failure(self):
        on_line = gate_line_observer(self.log, attempt=1)
        on_line("FAILED tests/test_x.py::test_a - AssertionError")
        self.assertEqual(self.kinds(), ["gate.line", "gate.test.fail"])
        self.assertEqual(self.seen[1]["payload"]["test"], "tests/test_x.py::test_a")
        self.assertEqual(self.seen[1]["attempt"], 1)

    def test_ordinary_line_emits_only_line(self):
        on_line = gate_line_observer(self.log)
        on_line("collected 12 items")
        self.assertEqual(self.kinds(), ["gate.line"])

    def test_lines_can_be_muted_keeping_failures(self):
        on_line = gate_line_observer(self.log, emit_lines=False)
        on_line("collected 12 items")
        on_line("--- FAIL: TestSignup (0.00s)")
        self.assertEqual(self.kinds(), ["gate.test.fail"])


class LocalGateStreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-lgate-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.py").write_text("V = 1\n")

    def test_failing_gate_streams_lines_and_failures(self):
        lines: list[str] = []
        res = _run_gate(
            self.repo,
            {"src/a.py": b"V = 2\n"},
            'echo "FAILED tests/test_x.py::test_a - boom"; exit 1',
            timeout=30,
            on_line=lines.append,
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.exit_code, 1)
        self.assertIn("FAILED tests/test_x.py::test_a", res.output_tail)
        self.assertTrue(any("FAILED tests/test_x.py::test_a" in ln for ln in lines))

    def test_passing_gate(self):
        res = _run_gate(self.repo, {"src/a.py": b"V = 2\n"}, "echo ok", timeout=30)
        self.assertTrue(res.passed)
        self.assertEqual(res.exit_code, 0)

    def test_timeout_reports_infra_error(self):
        res = _run_gate(self.repo, {"src/a.py": b"V = 2\n"}, "sleep 30", timeout=1)
        self.assertFalse(res.passed)
        self.assertIn("timed out", res.infra_error or "")


_real_run = subprocess.run
_real_popen = subprocess.Popen


def _fake_ssh_base(_cfg):
    return ["__FAKESSH__"]


def _patched_run(argv, **kw):
    if isinstance(argv, list) and argv and argv[0] == "__FAKESSH__":
        return _real_run(["sh", "-c", " ".join(argv[1:])], **kw)
    return _real_run(argv, **kw)


def _patched_popen(argv, **kw):
    if isinstance(argv, list) and argv and argv[0] == "__FAKESSH__":
        return _real_popen(["sh", "-c", " ".join(argv[1:])], **kw)
    return _real_popen(argv, **kw)


class RemoteGateStreamingTests(unittest.TestCase):
    def setUp(self):
        RG._ssh_base = _fake_ssh_base
        RG.subprocess.run = _patched_run
        PS.subprocess.Popen = _patched_popen

    def tearDown(self):
        RG.subprocess.run = _real_run
        PS.subprocess.Popen = _real_popen

    def test_remote_test_step_streams_lines(self):
        warm = Path(tempfile.mkdtemp())
        (warm / "src").mkdir()
        (warm / "src" / "a.py").write_text("V = 1\n")
        cfg = parse_remote_config({
            "ssh": "root@host",
            "repo_dir": str(warm),
            "copy_cmd": "cp -R {repo} {workdir}",
            "test_cmd": 'echo "FAILED tests/test_x.py::test_a - remote boom"; exit 1',
            "workdir_base": tempfile.mkdtemp(),
        }, "python")
        assert cfg is not None
        lines: list[str] = []
        res = run_remote_gate(cfg, {"src/a.py": b"V = 2\n"}, timeout=30, on_line=lines.append)
        self.assertFalse(res.passed)
        self.assertEqual(res.exit_code, 1)
        self.assertIn("FAILED tests/test_x.py::test_a", res.output_tail)
        self.assertTrue(any("remote boom" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
