"""Tests for the three adoptions from the cate study: dependency-dir symlinks
in the staged gate (bug fix), desktop notifications, and the status command.
Runnable with: python -m unittest tests.test_catestudy
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


class GateDepDirsTests(unittest.TestCase):
    """The staged gate copy excludes node_modules/.venv (state hygiene) but the
    suite NEEDS them — they must be symlinked from the real repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-deps-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "node_modules" / "leftpad").mkdir(parents=True)
        (self.repo / "node_modules" / "leftpad" / "index.js").write_text("module.exports=1\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_dep.py").write_text(
            "import pathlib, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_dep(self):\n"
            "        assert pathlib.Path('node_modules/leftpad/index.js').exists()\n"
        )

    def test_plain_materialize_gate_sees_deps(self):
        from shepherd_dev.supervisor import _run_gate

        res = _run_gate(
            self.repo, {"x.py": b"X = 1\n"},
            "python3 -m unittest -q tests.test_dep", 60,
        )
        self.assertTrue(res.passed, res.output_tail)

    def test_staged_gate_sees_deps(self):
        from shepherd_dev.supervisor import LocalGateStage, _run_gate

        stage = LocalGateStage(self.repo).start()
        res = _run_gate(
            self.repo, {"x.py": b"X = 1\n"},
            "python3 -m unittest -q tests.test_dep", 60, warmup=stage,
        )
        self.assertTrue(res.passed, res.output_tail)

    def test_dep_link_is_a_symlink_not_a_copy(self):
        from shepherd_dev.supervisor import _materialize

        dest = Path(self.tmp.name) / "dest"
        _materialize(self.repo, {}, dest)
        link = dest / "node_modules"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (self.repo / "node_modules").resolve())

    def test_absent_dep_dirs_are_not_linked(self):
        from shepherd_dev.supervisor import _materialize

        bare = Path(self.tmp.name) / "bare"
        (bare / "src").mkdir(parents=True)
        (bare / "src" / "a.py").write_text("A = 1\n")
        dest = Path(self.tmp.name) / "bare-dest"
        _materialize(bare, {}, dest)
        self.assertFalse((dest / "node_modules").exists())
        self.assertFalse((dest / ".venv").exists())


class NotifyTests(unittest.TestCase):
    def setUp(self):
        from shepherd_dev import notify as N

        self.N = N
        self.calls: list[list[str]] = []
        self._old_run = N.subprocess.run

        def fake_run(argv, **kw):
            self.calls.append(list(argv))

            class _P:
                returncode = 0

            return _P()

        N.subprocess.run = fake_run
        self.addCleanup(setattr, N.subprocess, "run", self._old_run)

    def test_notifies_via_platform_command(self):
        self.N.notify("shepherd-dev", "proposal ready — settle run-abc")
        self.assertEqual(len(self.calls), 1)
        joined = " ".join(self.calls[0])
        self.assertTrue(
            "osascript" in joined or "notify-send" in joined, self.calls[0]
        )
        self.assertIn("proposal ready", joined)

    def test_opt_out_env(self):
        import os

        os.environ["SHEPHERD_DEV_NO_NOTIFY"] = "1"
        self.addCleanup(os.environ.pop, "SHEPHERD_DEV_NO_NOTIFY", None)
        self.N.notify("t", "m")
        self.assertEqual(self.calls, [])

    def test_never_raises(self):
        def boom(argv, **kw):
            raise OSError("no notifier")

        self.N.subprocess.run = boom
        self.N.notify("t", "m")  # must not raise

    def test_message_quoting_is_safe(self):
        self.N.notify("t", 'tricky "quotes" and \\ backslashes')
        self.assertEqual(len(self.calls), 1)  # composed without shell explosion


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-status-")
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name)

    def _mk_run(self, run_id: str, events: list[dict]):
        d = self.runs / run_id
        d.mkdir(parents=True)
        with open(d / "events.ndjson", "w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")

    def test_states_finished_running_and_stale(self):
        from shepherd_dev.status import runs_status

        now = time.time()
        self._mk_run("20260721-100000-aaaaaa", [
            {"ts": now - 60, "seq": 1, "kind": "phase.start",
             "payload": {"label": "worker"}, "attempt": 1},
            {"ts": now - 5, "seq": 2, "kind": "run.summary",
             "payload": {"succeeded": True, "final_run_ref": "run-1", "feature": "feat X"}},
        ])
        self._mk_run("20260721-100100-bbbbbb", [
            {"ts": now - 30, "seq": 1, "kind": "phase.start",
             "payload": {"label": "worker"}, "attempt": 1},
            {"ts": now - 2, "seq": 2, "kind": "worker.tool", "payload": {"tool": "Edit"}},
        ])
        self._mk_run("20260721-090000-cccccc", [
            {"ts": now - 7200, "seq": 1, "kind": "phase.start",
             "payload": {"label": "gate"}, "attempt": 2},
        ])
        rows = {r["run_id"]: r for r in runs_status(root=self.runs)}
        self.assertEqual(rows["20260721-100000-aaaaaa"]["state"], "succeeded")
        self.assertEqual(rows["20260721-100000-aaaaaa"]["feature"], "feat X")
        running = rows["20260721-100100-bbbbbb"]
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["phase"], "worker")
        self.assertEqual(running["attempt"], 1)
        self.assertGreater(running["elapsed_s"], 25)
        self.assertEqual(rows["20260721-090000-cccccc"]["state"], "stale")

    def test_failed_run(self):
        from shepherd_dev.status import runs_status

        self._mk_run("20260721-110000-dddddd", [
            {"ts": time.time(), "seq": 1, "kind": "run.summary",
             "payload": {"succeeded": False}},
        ])
        rows = runs_status(root=self.runs)
        self.assertEqual(rows[0]["state"], "failed")

    def test_limit_and_order_newest_first(self):
        from shepherd_dev.status import runs_status

        for i in range(4):
            self._mk_run(f"20260721-12000{i}-eeeee{i}", [
                {"ts": time.time(), "seq": 1, "kind": "run.summary",
                 "payload": {"succeeded": True}},
            ])
        rows = runs_status(root=self.runs, limit=2)
        self.assertEqual(len(rows), 2)
        self.assertGreater(rows[0]["run_id"], rows[1]["run_id"])

    def test_empty_dir(self):
        from shepherd_dev.status import runs_status

        self.assertEqual(runs_status(root=self.runs), [])


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class CmdStatusTests(unittest.TestCase):
    def test_cmd_status_json(self):
        import contextlib
        import io
        import os
        from types import SimpleNamespace

        from shepherd_dev.cli import cmd_status

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SHEPHERD_DEV_RUNS_DIR"] = tmp
            try:
                d = Path(tmp) / "20260721-130000-ffffff"
                d.mkdir()
                (d / "events.ndjson").write_text(json.dumps({
                    "ts": time.time(), "seq": 1, "kind": "run.summary",
                    "payload": {"succeeded": True, "feature": "f"},
                }) + "\n")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = cmd_status(SimpleNamespace(json=True, limit=10, repo=None))
                self.assertEqual(code, 0)
                rows = json.loads(buf.getvalue())
                self.assertEqual(rows[0]["state"], "succeeded")
            finally:
                os.environ.pop("SHEPHERD_DEV_RUNS_DIR", None)


if __name__ == "__main__":
    unittest.main()
