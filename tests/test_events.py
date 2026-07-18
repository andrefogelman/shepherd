"""Tests for the per-run event log, the worker stream tailer, the edit-hunk
builder, and the test-failure line parsers (verbose mode core).
Runnable with: python -m unittest tests.test_events
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.events import (  # noqa: E402
    RunEventLog,
    StreamTailer,
    edit_hunk,
    latest_run_id,
    load_run_events,
    new_run_id,
    parse_test_failure,
)


class RunIdTests(unittest.TestCase):
    def test_shape_and_uniqueness(self):
        a, b = new_run_id(), new_run_id()
        self.assertRegex(a, r"^\d{8}-\d{6}-[0-9a-f]{6}$")
        self.assertNotEqual(a, b)


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-events-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_emit_writes_ndjson_with_seq(self):
        log = RunEventLog(run_id="t1", root=self.root)
        log.emit("phase.start", {"label": "worker"}, attempt=1)
        log.emit("phase.done", {"label": "worker"})
        lines = log.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first, second = (json.loads(ln) for ln in lines)
        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["seq"], 2)
        self.assertEqual(first["kind"], "phase.start")
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["payload"], {"label": "worker"})
        self.assertIn("ts", first)

    def test_emit_never_raises_on_unserializable_payload(self):
        log = RunEventLog(run_id="t2", root=self.root)
        event = log.emit("worker.tool", {"obj": object()})
        self.assertEqual(event["kind"], "worker.tool")
        line = json.loads(log.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(line["kind"], "worker.tool")  # written despite the payload

    def test_observer_receives_events_and_errors_are_swallowed(self):
        log = RunEventLog(run_id="t3", root=self.root)
        seen: list[dict] = []

        def boom(_event):
            raise RuntimeError("observer bug")

        log.subscribe(boom)
        log.subscribe(seen.append)
        log.emit("gate.line", {"line": "ok"})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["kind"], "gate.line")

    def test_path_layout(self):
        log = RunEventLog(run_id="t4", root=self.root)
        self.assertEqual(log.path, self.root / "t4" / "events.ndjson")

    def test_log_files_are_private(self):
        # Hunks and gate output can carry secrets — 0700 dir, 0600 file.
        import stat

        log = RunEventLog(run_id="t6", root=self.root)
        log.emit("phase.start", {"label": "worker"})
        self.assertEqual(log.dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(log.path.stat().st_mode & 0o777, 0o600)
        del stat

    def test_load_and_latest(self):
        older = RunEventLog(run_id="20260101-000000-aaaaaa", root=self.root)
        older.emit("run.summary", {"n": 1})
        newer = RunEventLog(run_id="20260102-000000-bbbbbb", root=self.root)
        newer.emit("run.summary", {"n": 2})
        self.assertEqual(latest_run_id(root=self.root), "20260102-000000-bbbbbb")
        events = load_run_events("20260101-000000-aaaaaa", root=self.root)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"], {"n": 1})

    def test_load_tolerates_bad_lines(self):
        log = RunEventLog(run_id="t5", root=self.root)
        log.emit("phase.start", {"label": "gate"})
        with open(log.path, "a", encoding="utf-8") as fh:
            fh.write("not json\n")
        events = load_run_events("t5", root=self.root)
        self.assertEqual(len(events), 1)


class EditHunkTests(unittest.TestCase):
    def test_counts_and_hunk(self):
        h = edit_hunk("a\nb\nc\n", "a\nB\nc\nd\n", path="src/a.py")
        self.assertEqual(h["add"], 2)
        self.assertEqual(h["del"], 1)
        self.assertIn("-b", h["hunk"])
        self.assertIn("+B", h["hunk"])
        self.assertIn("+d", h["hunk"])

    def test_hunk_is_truncated(self):
        old = "\n".join(f"line {i}" for i in range(4000))
        new = "\n".join(f"LINE {i}" for i in range(4000))
        h = edit_hunk(old, new, path="big.txt")
        self.assertLessEqual(len(h["hunk"]), 4200)
        self.assertTrue(h["hunk"].endswith("…") or len(h["hunk"]) < 4200)


class TestFailureParserTests(unittest.TestCase):
    CASES = (
        ("FAILED tests/test_x.py::test_a - AssertionError", "pytest", "tests/test_x.py::test_a"),
        ("tests/test_x.py::test_a FAILED [ 50%]", "pytest", "tests/test_x.py::test_a"),
        ("FAIL: test_a (tests.test_x.TC.test_a)", "unittest", "test_a"),
        ("  ✕ renders the widget (23 ms)", "jest", "renders the widget"),
        ("  1) test signup validates document (MyApp.SignupTest)", "exunit",
         "signup validates document (MyApp.SignupTest)"),
        ("test cpf::checks ... FAILED", "cargo", "cpf::checks"),
        ("--- FAIL: TestSignup (0.00s)", "go", "TestSignup"),
    )

    def test_known_failure_lines(self):
        for line, framework, test in self.CASES:
            got = parse_test_failure(line)
            self.assertIsNotNone(got, f"no match: {line!r}")
            self.assertEqual(got["framework"], framework, line)
            self.assertEqual(got["test"], test, line)

    def test_non_failure_lines_return_none(self):
        for line in (
            "collected 12 items",
            "tests/test_x.py::test_a PASSED",
            "test cpf::checks ... ok",
            "--- PASS: TestSignup (0.00s)",
            "= 1 failed, 3 passed in 0.21s =",
            "",
        ):
            self.assertIsNone(parse_test_failure(line), line)


def _claude_event(*blocks) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


class StreamTailerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-tailer-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stream = self.root / "worker-stream.ndjson"
        self.log = RunEventLog(run_id="tail", root=self.root)
        self.seen: list[dict] = []
        self.log.subscribe(self.seen.append)

    def _tailer(self, **kw) -> StreamTailer:
        kw.setdefault("poll_interval", 0.01)
        return StreamTailer(self.stream, self.log, **kw)

    def _wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def kinds(self):
        return [e["kind"] for e in self.seen]

    def test_tool_use_edit_and_write(self):
        tailer = self._tailer()
        tailer.start()
        try:
            with open(self.stream, "w", encoding="utf-8") as fh:
                fh.write(_claude_event({
                    "type": "tool_use", "id": "t1", "name": "Edit",
                    "input": {"file_path": "src/a.py", "old_string": "x = 1\n", "new_string": "x = 2\n"},
                }) + "\n")
                fh.flush()
                fh.write(_claude_event({
                    "type": "tool_use", "id": "t2", "name": "Write",
                    "input": {"file_path": "src/b.py", "content": "print('hi')\nprint('bye')\n"},
                }) + "\n")
            self.assertTrue(self._wait_for(lambda: "worker.write" in self.kinds()))
        finally:
            tailer.drain()
        edits = [e for e in self.seen if e["kind"] == "worker.edit"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["payload"]["path"], "src/a.py")
        self.assertEqual(edits[0]["payload"]["add"], 1)
        self.assertEqual(edits[0]["payload"]["del"], 1)
        self.assertIn("-x = 1", edits[0]["payload"]["hunk"])
        writes = [e for e in self.seen if e["kind"] == "worker.write"]
        self.assertEqual(writes[0]["payload"]["path"], "src/b.py")
        self.assertEqual(writes[0]["payload"]["lines"], 2)
        tools = [e for e in self.seen if e["kind"] == "worker.tool"]
        self.assertEqual([t["payload"]["tool"] for t in tools], ["Edit", "Write"])

    def test_partial_line_across_writes(self):
        tailer = self._tailer()
        tailer.start()
        try:
            line = _claude_event({
                "type": "tool_use", "id": "t1", "name": "Read",
                "input": {"file_path": "src/a.py"},
            }) + "\n"
            half = len(line) // 2
            with open(self.stream, "w", encoding="utf-8") as fh:
                fh.write(line[:half])
                fh.flush()
                time.sleep(0.1)
                fh.write(line[half:])
            self.assertTrue(self._wait_for(lambda: "worker.tool" in self.kinds()))
        finally:
            tailer.drain()
        tools = [e for e in self.seen if e["kind"] == "worker.tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["payload"]["tool"], "Read")

    def test_late_file_creation(self):
        tailer = self._tailer()
        tailer.start()  # file does not exist yet
        try:
            time.sleep(0.05)
            with open(self.stream, "w", encoding="utf-8") as fh:
                fh.write(_claude_event({"type": "text", "text": "thinking about the fix"}) + "\n")
            self.assertTrue(self._wait_for(lambda: "worker.note" in self.kinds()))
        finally:
            tailer.drain()

    def test_tool_result_error(self):
        tailer = self._tailer()
        tailer.start()
        try:
            with open(self.stream, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "user",
                    "message": {"content": [{
                        "type": "tool_result", "tool_use_id": "t1",
                        "is_error": True, "content": "No such file or directory",
                    }]},
                }) + "\n")
            self.assertTrue(self._wait_for(lambda: "worker.tool.fail" in self.kinds()))
        finally:
            tailer.drain()
        fails = [e for e in self.seen if e["kind"] == "worker.tool.fail"]
        self.assertIn("No such file", fails[0]["payload"]["error"])

    def test_drain_reads_remainder_without_newline(self):
        tailer = self._tailer()
        tailer.start()
        with open(self.stream, "w", encoding="utf-8") as fh:
            fh.write(_claude_event({
                "type": "tool_use", "id": "t9", "name": "Bash",
                "input": {"command": "python -m unittest"},
            }))  # NO trailing newline
        tailer.drain()
        tools = [e for e in self.seen if e["kind"] == "worker.tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["payload"]["tool"], "Bash")

    def test_oversize_line_is_flagged_not_fatal(self):
        tailer = self._tailer(max_line_bytes=500)
        tailer.start()
        try:
            with open(self.stream, "w", encoding="utf-8") as fh:
                fh.write(_claude_event({
                    "type": "tool_use", "id": "big", "name": "Write",
                    "input": {"file_path": "big.txt", "content": "A" * 5000},
                }) + "\n")
                fh.write(_claude_event({"type": "text", "text": "still alive"}) + "\n")
            self.assertTrue(self._wait_for(lambda: "worker.note" in self.kinds()))
        finally:
            tailer.drain()
        self.assertIn("worker.raw", self.kinds())  # truncation marker
        raws = [e for e in self.seen if e["kind"] == "worker.raw"]
        self.assertTrue(raws[0]["payload"]["truncated"])

    def test_write_diffs_against_baseline_when_available(self):
        baseline = {"src/b.py": "print('hi')\n"}
        tailer = self._tailer(read_baseline=baseline.get)
        tailer.start()
        try:
            with open(self.stream, "w", encoding="utf-8") as fh:
                fh.write(_claude_event({
                    "type": "tool_use", "id": "t2", "name": "Write",
                    "input": {"file_path": "src/b.py", "content": "print('hi')\nprint('bye')\n"},
                }) + "\n")
            self.assertTrue(self._wait_for(lambda: "worker.write" in self.kinds()))
        finally:
            tailer.drain()
        writes = [e for e in self.seen if e["kind"] == "worker.write"]
        self.assertEqual(writes[0]["payload"]["add"], 1)
        self.assertEqual(writes[0]["payload"]["del"], 0)
        self.assertIn("+print('bye')", writes[0]["payload"]["hunk"])


if __name__ == "__main__":
    unittest.main()
