"""`status` reports how the agents spent their turns: exploring before the
first edit, and navigating during review.

Runnable with: python -m unittest tests.test_status_metrics
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class StatusBehaviourMetrics(unittest.TestCase):
    def test_explore_and_review_tool_counts(self):
        from shepherd_dev.events import RunEventLog, load_run_events
        from shepherd_dev.status import behaviour_metrics, render_status, runs_status

        root = Path(tempfile.mkdtemp(prefix="shepherd-status-"))
        log = RunEventLog(run_id="20260901-000000-abc123", root=root)
        log.emit("phase.start", {"label": "worker"}, attempt=1)
        for tool in ("Bash", "Read", "Read", "Grep"):
            log.emit("worker.tool", {"tool": tool}, attempt=1)
        log.emit("worker.tool", {"tool": "Edit"}, attempt=1)
        log.emit("worker.tool", {"tool": "Read"}, attempt=1)  # after the first edit: not exploring
        log.emit("phase.start", {"label": "gate"}, attempt=1)
        log.emit("phase.start", {"label": "review"}, attempt=1)
        for _ in range(7):
            log.emit("worker.tool", {"tool": "Bash"}, attempt=1)
        log.emit("run.summary", {"succeeded": True})
        events = load_run_events("20260901-000000-abc123", root=root)
        self.assertEqual(behaviour_metrics(events), {"explore_calls": 4, "review_tools": 7})
        rows = runs_status(root=root)
        self.assertEqual(rows[0]["explore_calls"], 4)
        self.assertIn("explore 4 · review tools 7", render_status(rows)[0])

    def test_no_tools_no_metrics(self):
        from shepherd_dev.status import behaviour_metrics

        self.assertEqual(behaviour_metrics([{"kind": "phase.start", "payload": {"label": "worker"}}]), {})



if __name__ == "__main__":
    unittest.main()
