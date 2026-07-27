"""Tests for gate output streaming (Fase 3 of verbose mode): the line-streamed
subprocess runner with process-group kill, the gate line observer, and the
local + remote gates emitting per-line / per-failure events.
Runnable with: python -m unittest tests.test_gatestream
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev import procstream as PS  # noqa: E402
from shepherd_dev import remotegate as RG  # noqa: E402
from shepherd_dev.events import RunEventLog, gate_line_observer  # noqa: E402
from shepherd_dev.procstream import run_streaming  # noqa: E402
from shepherd_dev.remotegate import parse_remote_config, run_remote_gate  # noqa: E402
from shepherd_dev.supervisor import _run_gate  # noqa: E402


class GateEnvIsolation(unittest.TestCase):
    """#4: the gate judges the PROPOSAL's tree. Inheriting the supervisor's
    whole environment hands it interpreter state pointing at the supervisor's
    own checkout — PYTHONPATH/VIRTUAL_ENV/PYTEST_* — so the suite can resolve
    imports and harness config from outside the tree under test."""

    def setUp(self):
        self.repo = Path(mkdtemp(prefix="shepherd-gateenv-"))
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        pass\n"
        )

    def _dump_env(self, name: str) -> str:
        entries = {"tests/test_x.py": (self.repo / "tests" / "test_x.py").read_bytes()}
        res = _run_gate(self.repo, entries, f'printf "%s" "{name}-<${{{name}:-UNSET}}>"', 60)
        return res.output_tail

    def test_interpreter_state_is_stripped(self):
        for var in ("PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME", "PYTEST_CURRENT_TEST"):
            with self.subTest(var=var):
                old = os.environ.get(var)
                os.environ[var] = "/leaked/from/the/supervisor"
                try:
                    self.assertIn(f"{var}-<UNSET>", self._dump_env(var))
                finally:
                    if old is None:
                        os.environ.pop(var, None)
                    else:
                        os.environ[var] = old

    def test_ordinary_environment_still_reaches_the_gate(self):
        # A blanket scrub would break every real gate: PATH, HOME and the
        # project's own variables must survive.
        old = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "postgres://gate"
        try:
            self.assertIn("DATABASE_URL-<postgres://gate>", self._dump_env("DATABASE_URL"))
            self.assertNotIn("PATH-<UNSET>", self._dump_env("PATH"))
        finally:
            if old is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old

    def test_the_stage_is_removed_even_under_a_broken_pythonhome(self):
        """_remove_tree deletes the staged tree from a CHILD interpreter. That
        child is ours, so a PYTHONHOME aimed elsewhere stops it booting — and
        with check=False the failure is invisible and every staged tree leaks."""
        import tempfile as _tf

        scratch = Path(mkdtemp(prefix="shepherd-removeprobe-"))
        before = set(Path(_tf.gettempdir()).glob("shepherd-gate-*"))
        old = os.environ.get("PYTHONHOME")
        os.environ["PYTHONHOME"] = "/nonexistent/interpreter"
        try:
            entries = {"tests/test_x.py": (self.repo / "tests" / "test_x.py").read_bytes()}
            _run_gate(self.repo, entries, "true", 60)
        finally:
            if old is None:
                os.environ.pop("PYTHONHOME", None)
            else:
                os.environ["PYTHONHOME"] = old
            shutil.rmtree(scratch, ignore_errors=True)
        after = set(Path(_tf.gettempdir()).glob("shepherd-gate-*"))
        self.assertEqual(after - before, set(), "the gate's staged tree leaked")

    def test_strip_list_is_configurable_in_both_directions(self):
        from shepherd_dev.supervisor import gate_env

        base = {"PYTHONPATH": "/x", "MY_VAR": "keep", "PATH": "/bin"}
        self.assertNotIn("PYTHONPATH", gate_env(base))
        self.assertEqual(gate_env(base)["MY_VAR"], "keep")
        # opt back in when a gate genuinely needs the parent's PYTHONPATH
        self.assertEqual(gate_env(base, keep=["PYTHONPATH"])["PYTHONPATH"], "/x")
        # ...and opt more out
        self.assertNotIn("MY_VAR", gate_env(base, strip=["MY_VAR"]))


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
        warm = Path(mkdtemp())
        (warm / "src").mkdir()
        (warm / "src" / "a.py").write_text("V = 1\n")
        cfg = parse_remote_config({
            "ssh": "root@host",
            "repo_dir": str(warm),
            "copy_cmd": "cp -R {repo} {workdir}",
            "test_cmd": 'echo "FAILED tests/test_x.py::test_a - remote boom"; exit 1',
            "workdir_base": mkdtemp(),
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
