"""Every launch reports what it cost and what model served it.

The CLI's stream-json ends with a `result` event carrying usage, cost, turns
and durations, and opens with a `system/init` event naming the model, the
tools and the MCP servers. Both flowed through the tee and were dropped, so
274 recorded runs held not one token count and no evidence of which model
ran. Duration was the only proxy anyone had.

Runnable with: python -m unittest tests.test_telemetry
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
    WorkerStreamHook,
    format_usage,
    load_run_events,
    session_init_info,
    session_result_info,
)

INIT = {
    "type": "system", "subtype": "init", "cwd": "/w", "session_id": "s1",
    "tools": ["Bash", "Read", "Edit", "Write"], "mcp_servers": [],
    "model": "claude-opus-4-8", "permissionMode": "bypassPermissions",
    "claude_code_version": "2.1.258",
}
RESULT = {
    "type": "result", "subtype": "success", "is_error": False,
    "duration_ms": 61234, "duration_api_ms": 48000, "num_turns": 12,
    "result": "Done.", "session_id": "s1", "total_cost_usd": 0.4137,
    "usage": {
        "input_tokens": 45210, "output_tokens": 3120,
        "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 2000,
        "server_tool_use": {"web_search_requests": 0}, "service_tier": "standard",
    },
    "modelUsage": {"claude-opus-4-8": {"inputTokens": 45210, "outputTokens": 3120, "costUSD": 0.4137}},
}


class InfoExtraction(unittest.TestCase):
    def test_init_names_model_tools_and_servers(self):
        info = session_init_info({**INIT, "mcp_servers": [{"name": "supabase", "status": "connected"}]})
        self.assertEqual(info["model"], "claude-opus-4-8")
        self.assertEqual(info["tools"], 4)
        self.assertEqual(info["mcp_servers"], ["supabase"])
        self.assertEqual(info["cli_version"], "2.1.258")

    def test_result_keeps_numbers_and_drops_the_text(self):
        info = session_result_info(RESULT)
        self.assertEqual(info["input_tokens"], 45210)
        self.assertEqual(info["output_tokens"], 3120)
        self.assertEqual(info["cache_read_input_tokens"], 30000)
        self.assertEqual(info["cache_creation_input_tokens"], 2000)
        self.assertEqual(info["total_cost_usd"], 0.4137)
        self.assertEqual(info["num_turns"], 12)
        self.assertEqual(info["duration_api_ms"], 48000)
        self.assertEqual(info["model"], "claude-opus-4-8")
        self.assertEqual(info["models"], ["claude-opus-4-8"])
        self.assertNotIn("result", info)
        self.assertNotIn("Done.", json.dumps(info))

    def test_a_bare_result_yields_an_empty_record_not_a_crash(self):
        self.assertEqual(session_result_info({"type": "result"}), {})
        self.assertEqual(session_result_info({"type": "result", "usage": "garbage"}), {})

    def test_format_usage_reads_as_one_line(self):
        line = format_usage(session_result_info(RESULT))
        self.assertEqual(line, "45,210 in / 3,120 out (+30,000 cached) · 12 turns · claude-opus-4-8 · $0.41")
        self.assertEqual(format_usage(None), "")
        self.assertEqual(format_usage({}), "")


class TheTailerRecordsBothEvents(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shepherd-telemetry-"))
        self.log = RunEventLog(run_id="r1", root=self.root)
        self.tee = self.root / "tee.ndjson"

    def _events(self):
        return load_run_events("r1", root=self.root)

    def test_init_and_result_become_events_and_fill_the_slot(self):
        slot: dict = {}
        tailer = StreamTailer(self.tee, self.log, attempt=1, slot=slot, poll_interval=0.01)
        tailer.start()
        with open(self.tee, "w") as fh:
            fh.write(json.dumps(INIT) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n")
            fh.write(json.dumps(RESULT) + "\n")
        time.sleep(0.1)
        tailer.drain()
        kinds = [e["kind"] for e in self._events()]
        self.assertEqual(kinds, ["worker.init", "worker.note", "worker.result"])
        init = next(e for e in self._events() if e["kind"] == "worker.init")
        self.assertEqual(init["payload"]["model"], "claude-opus-4-8")
        self.assertEqual(init["attempt"], 1)
        result = next(e for e in self._events() if e["kind"] == "worker.result")
        self.assertEqual(result["payload"]["num_turns"], 12)
        self.assertEqual(slot["result"]["total_cost_usd"], 0.4137)
        self.assertEqual(slot["init"]["model"], "claude-opus-4-8")

    def test_the_model_comes_from_init_when_result_has_no_model_usage(self):
        slot: dict = {}
        tailer = StreamTailer(self.tee, self.log, slot=slot, poll_interval=0.01)
        tailer.start()
        bare = {k: v for k, v in RESULT.items() if k != "modelUsage"}
        with open(self.tee, "w") as fh:
            fh.write(json.dumps(INIT) + "\n" + json.dumps(bare) + "\n")
        time.sleep(0.1)
        tailer.drain()
        result = next(e for e in self._events() if e["kind"] == "worker.result")
        self.assertEqual(result["payload"]["model"], "claude-opus-4-8")


class TheHookHandsTheResultBackToTheLaunchingThread(unittest.TestCase):
    def test_take_result_returns_once_and_only_for_this_thread(self):
        root = Path(tempfile.mkdtemp(prefix="shepherd-telemetry-"))
        log = RunEventLog(run_id="r2", root=root)
        hook = WorkerStreamHook(log)
        work = root / "w"
        (work / ".claude-scratch" / "tmp").mkdir(parents=True)
        tailer = hook.start(work)
        self.assertIsNotNone(tailer)
        with open(hook.tee_path(work), "w") as fh:
            fh.write(json.dumps(INIT) + "\n" + json.dumps(RESULT) + "\n")
        time.sleep(0.15)
        hook.drain(tailer)
        usage = hook.take_result()
        self.assertIsNotNone(usage)
        self.assertEqual(usage["output_tokens"], 3120)
        self.assertEqual(usage["model"], "claude-opus-4-8")
        # consumed: a later launch on this thread cannot inherit these numbers
        self.assertIsNone(hook.take_result())

    def test_no_launch_no_result(self):
        self.assertIsNone(WorkerStreamHook(None).take_result())


class DevelopRecordsUsagePerAttemptAndForTheReview(unittest.TestCase):
    def _run(self):
        from shepherd_dev import supervisor as sup

        class _Hook:
            """Stands in for WorkerStreamHook: hands back one canned result
            per take, in launch order (worker, then reviewer)."""

            def __init__(self):
                self.queue = [
                    {"input_tokens": 100, "output_tokens": 10, "num_turns": 3, "model": "m-worker", "total_cost_usd": 0.1},
                    {"input_tokens": 200, "output_tokens": 20, "num_turns": 5, "model": "m-review", "total_cost_usd": 0.2},
                ]

            def set_attempt(self, n):
                pass

            def take_result(self):
                return self.queue.pop(0) if self.queue else None

        class _Output:
            def changeset(self):
                return {"file.py": b"v1\n"}

            def discard(self):
                pass

        class _Run:
            run_ref = "run-1"

            def output(self):
                return _Output()

        class _Tasks:
            def register(self, task):
                pass

        class _Workspace:
            tasks = _Tasks()

            def run(self, task, **kw):
                return _Run()

        orig = (sup.read_changeset_entries, sup._run_gate, sup.run_review, sup._start_gate_warmup)
        sup.read_changeset_entries = lambda cs: dict(cs)
        sup._run_gate = lambda *a, **k: sup.GateResult(True, 0, "ok")
        sup.run_review = lambda ws, task, **kw: sup.ReviewVerdict(approved=True, summary="fine")
        sup._start_gate_warmup = lambda *a, **k: None
        try:
            return sup.develop(
                _Workspace(), object(), repo="R", repo_root=Path("/r"), feature="f",
                test_cmd="true", review_task=object(), max_attempts=1, stream_hook=_Hook(),
            )
        finally:
            sup.read_changeset_entries, sup._run_gate, sup.run_review, sup._start_gate_warmup = orig

    def test_attempt_and_review_carry_their_own_usage(self):
        report = self._run()
        self.assertEqual(report.attempts[0].usage["model"], "m-worker")
        self.assertEqual(report.attempts[0].usage["num_turns"], 3)
        self.assertEqual(report.review.usage["model"], "m-review")

    def test_both_renderers_and_the_history_show_it(self):
        from shepherd_dev.history import run_payload
        from shepherd_dev.supervisor import render_review_report

        report = self._run()
        summary = report.summary()
        self.assertIn("usage: 100 in / 10 out · 3 turns · m-worker · $0.10", summary)
        self.assertIn("usage: 200 in / 20 out · 5 turns · m-review · $0.20", summary)
        rendered = render_review_report(report)
        self.assertIn("- usage: 100 in / 10 out · 3 turns · m-worker · $0.10", rendered)
        self.assertIn("- usage: 200 in / 20 out · 5 turns · m-review · $0.20", rendered)
        payload = run_payload(report, Path("/r"), mode="feature", test_cmd="true", provider="claude", flags={})
        self.assertEqual(payload["attempts"][0]["usage"]["input_tokens"], 100)
        self.assertEqual(payload["review"]["usage"]["input_tokens"], 200)


class StatusSumsARun(unittest.TestCase):
    def test_tokens_cost_and_models_are_summed_over_the_launches(self):
        from shepherd_dev.status import render_status, run_telemetry, runs_status

        root = Path(tempfile.mkdtemp(prefix="shepherd-telemetry-"))
        log = RunEventLog(run_id="20260901-000000-abcdef", root=root)
        log.emit("phase.start", {"label": "worker"}, attempt=1)
        log.emit("worker.result", {"input_tokens": 100, "output_tokens": 10, "total_cost_usd": 0.1, "model": "m1"})
        log.emit("phase.start", {"label": "review"}, attempt=1)
        log.emit("worker.result", {"input_tokens": 50, "output_tokens": 5, "total_cost_usd": 0.05, "model": "m2"})
        log.emit("run.summary", {"succeeded": True, "feature": "f"})
        events = load_run_events("20260901-000000-abcdef", root=root)
        t = run_telemetry(events)
        self.assertEqual(t["launches"], 2)
        self.assertEqual(t["tokens_in"], 150)
        self.assertEqual(t["tokens_out"], 15)
        self.assertEqual(t["cost_usd"], 0.15)
        self.assertEqual(t["models"], ["m1", "m2"])
        rows = runs_status(root=root)
        self.assertEqual(rows[0]["tokens_in"], 150)
        line = render_status(rows)[0]
        self.assertIn("150 in / 15 out · $0.15 · 2 launch(es) · m1, m2", line)

    def test_a_run_with_no_results_has_no_telemetry_keys(self):
        from shepherd_dev.status import run_telemetry

        self.assertEqual(run_telemetry([{"kind": "phase.start"}]), {})


class TraceRendersTheNewEvents(unittest.TestCase):
    def test_init_and_result_lines(self):
        from shepherd_dev.progress import format_event

        line = format_event({"kind": "worker.init", "payload": session_init_info(INIT)}, live=False)
        self.assertEqual(line, "⚙ session: model claude-opus-4-8 · 4 tools · MCP servers: none")
        line = format_event({"kind": "worker.result", "payload": session_result_info(RESULT)}, live=False)
        self.assertTrue(line.startswith("Σ 45,210 in / 3,120 out"))


if __name__ == "__main__":
    unittest.main()
