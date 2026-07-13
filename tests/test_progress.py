"""Tests for the live progress reporter (#A) and the post-hoc worker activity
summary (#B). Runnable with: python -m unittest tests.test_progress
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.progress import (  # noqa: E402
    NullProgress, ProgressReporter, worker_activity_summary,
)


class NonTtyReporter(unittest.TestCase):
    def _reporter(self):
        buf = io.StringIO()
        return buf, ProgressReporter(stream=buf, enabled=False)

    def test_step_commits_previous_and_starts_next(self):
        buf, r = self._reporter()
        r.step("attempt 1/3 · worker")
        r.step("attempt 1 · gate")   # finalizes worker as ✓
        out = buf.getvalue()
        self.assertIn("▶ attempt 1/3 · worker", out)
        self.assertIn("✓ attempt 1/3 · worker", out)
        self.assertIn("▶ attempt 1 · gate", out)

    def test_fail_marks_cross_with_reason(self):
        buf, r = self._reporter()
        r.step("attempt 1 · gate")
        r.fail("gate exit 1")
        out = buf.getvalue()
        self.assertIn("✗ attempt 1 · gate", out)
        self.assertIn("gate exit 1", out)

    def test_note_is_committed(self):
        buf, r = self._reporter()
        r.step("attempt 1 · worker")
        r.note("worker: 2 file(s): a.py, b.py")
        self.assertIn("worker: 2 file(s): a.py, b.py", buf.getvalue())

    def test_close_finalizes_last(self):
        buf, r = self._reporter()
        r.step("attempt 1 · review")
        r.close(ok=True)
        self.assertIn("✓ attempt 1 · review", buf.getvalue())


class TtyReporterLifecycle(unittest.TestCase):
    def test_ticker_starts_and_stops(self):
        buf = io.StringIO()
        r = ProgressReporter(stream=buf, enabled=True)
        r.step("worker running")
        self.assertIsNotNone(r._ticker)
        self.assertTrue(r._ticker.is_alive())
        r.close(ok=True)
        self.assertFalse(r._ticker.is_alive())     # ticker joined on close
        self.assertIn("worker running", buf.getvalue())


class NullReporter(unittest.TestCase):
    def test_all_noops(self):
        r = NullProgress()
        with r as ctx:
            ctx.step("x")
            ctx.note("y")
            ctx.fail("z")
        # nothing to assert beyond "does not raise"


class _Trace:
    def __init__(self, events):
        self.events = tuple(events)


class _Run:
    def __init__(self, trace=None):
        self.trace = trace


class WorkerActivitySummary(unittest.TestCase):
    def test_files_only_without_trace(self):
        s = worker_activity_summary(_Run(trace=None), {"a.py": b"1", "b.py": b"2"})
        self.assertIn("2 file(s): a.py, b.py", s)
        self.assertNotIn("tools:", s)

    def test_tool_tally_from_trace(self):
        trace = _Trace([
            {"kind": "tool.call", "payload": {"tool": "read"}},
            {"kind": "tool.call", "payload": {"tool": "read"}},
            {"kind": "tool.call", "payload": {"tool": "edit"}},
            {"kind": "run.lifecycle", "payload": {}},   # ignored
        ])
        s = worker_activity_summary(_Run(trace=trace), {"a.py": b"x"})
        self.assertIn("tools:", s)
        self.assertIn("2×read", s)
        self.assertIn("1×edit", s)

    def test_truncates_many_files(self):
        entries = {f"f{i}.py": b"x" for i in range(10)}
        s = worker_activity_summary(_Run(), entries)
        self.assertIn("10 file(s)", s)
        self.assertIn("…", s)

    def test_empty_entries(self):
        self.assertIn("no files", worker_activity_summary(_Run(), {}))

    def test_bad_trace_falls_back_to_files(self):
        class _Boom:
            @property
            def trace(self):
                raise RuntimeError("trace read blew up")
        s = worker_activity_summary(_Boom(), {"a.py": b"x"})
        self.assertIn("1 file(s): a.py", s)


if __name__ == "__main__":
    unittest.main()
