"""Tests for the zero-dep MCP stdio server (Codex/Cursor/Claude/ChatGPT desktop).

Exercises the JSON-RPC dispatch, the CLI argv each tool builds (with the forced
--no-settle so nothing is ever applied through MCP), and the stdio serve loop.
The CLI shell-out is faked. Runnable with: python -m unittest tests.test_mcpserver
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import shepherd_dev.mcpserver as M  # noqa: E402
from shepherd_dev.mcpserver import _argv_for, handle_message, serve  # noqa: E402


class JsonRpcDispatch(unittest.TestCase):
    def test_initialize_handshake(self):
        r = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r["id"], 1)
        self.assertIn("serverInfo", r["result"])
        self.assertEqual(r["result"]["serverInfo"]["name"], "shepherd-dev")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")

    def test_initialized_notification_no_reply(self):
        self.assertIsNone(handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_ping(self):
        r = handle_message({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(r["result"], {})

    def test_tools_list(self):
        r = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertEqual(names, {"shepherd_run", "shepherd_run2", "shepherd_settle", "shepherd_settle_par"})
        for t in r["result"]["tools"]:
            self.assertIn("inputSchema", t)
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_unknown_method_errors(self):
        r = handle_message({"jsonrpc": "2.0", "id": 3, "method": "nope/nope"})
        self.assertEqual(r["error"]["code"], -32601)

    def test_unknown_notification_ignored(self):
        self.assertIsNone(handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled"}))


class ToolArgv(unittest.TestCase):
    def test_run_forces_no_settle_and_flags(self):
        argv = _argv_for("shepherd_run", {
            "feature": "add CPF", "repo": "/x", "test_cmd": "pytest -q",
            "mode": "tests", "best_of": 3, "allowed_prefix": ["src/", "lib/"], "max_attempts": 2,
        })
        self.assertEqual(argv[:3], ["run", "add CPF", "--no-settle"])
        self.assertIn("--repo", argv); self.assertIn("/x", argv)
        self.assertIn("--test-cmd", argv); self.assertIn("pytest -q", argv)
        self.assertEqual(argv.count("--allowed-prefix"), 2)
        self.assertIn("tests", argv)  # --mode tests

    def test_run2_forces_no_settle(self):
        argv = _argv_for("shepherd_run2", {"feature_a": "A", "feature_b": "B"})
        self.assertEqual(argv[:4], ["run2", "A", "B", "--no-settle"])

    def test_best_of_clamped_to_2_4(self):  # #17
        self.assertIn("3", _argv_for("shepherd_run", {"feature": "x", "best_of": 3}))
        for bad in (1, 5, 7, 0):
            argv = _argv_for("shepherd_run", {"feature": "x", "best_of": bad})
            self.assertNotIn("--best-of", argv, f"best_of={bad} should be dropped, not leaked to the CLI")

    def test_settle_reject(self):
        argv = _argv_for("shepherd_settle", {"run_ref": "run-1", "repo": "/x", "reject": True})
        self.assertEqual(argv, ["settle", "run-1", "--repo", "/x", "--reject"])

    def test_settle_par_accept(self):
        argv = _argv_for("shepherd_settle_par", {"proposal_id": "p1"})
        self.assertEqual(argv, ["settle-par", "p1"])


class ToolCall(unittest.TestCase):
    def setUp(self):
        self._real = M._run_cli
        self.seen = {}

        def fake(argv, timeout=3600):
            self.seen["argv"] = argv
            return 0, "gate PASS · retained run-abc123"

        M._run_cli = fake

    def tearDown(self):
        M._run_cli = self._real

    def test_call_run_shells_out_and_returns_text(self):
        r = handle_message({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "shepherd_run", "arguments": {"feature": "x", "repo": "/r"}}})
        self.assertEqual(self.seen["argv"][:3], ["run", "x", "--no-settle"])
        self.assertFalse(r["result"]["isError"])
        self.assertIn("retained run-abc123", r["result"]["content"][0]["text"])

    def test_call_unknown_tool_is_error(self):
        r = handle_message({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                            "params": {"name": "shepherd_nope", "arguments": {}}})
        self.assertTrue(r["result"]["isError"])

    def test_call_missing_required_arg_is_error(self):
        r = handle_message({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": "shepherd_run", "arguments": {}}})
        self.assertTrue(r["result"]["isError"])

    def _call(self, name, args):
        return handle_message({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                               "params": {"name": name, "arguments": args}})

    def test_settle_requires_confirm(self):  # #4
        r = self._call("shepherd_settle", {"run_ref": "run-1"})
        self.assertFalse(r["result"]["isError"])
        self.assertIn("confirm=true", r["result"]["content"][0]["text"])
        self.assertNotIn("argv", self.seen)  # the CLI was NOT invoked — nothing applied

    def test_settle_with_confirm_applies(self):  # #4
        self._call("shepherd_settle", {"run_ref": "run-1", "repo": "/r", "confirm": True})
        self.assertEqual(self.seen["argv"][:2], ["settle", "run-1"])

    def test_settle_reject_needs_no_confirm(self):  # #4
        self._call("shepherd_settle", {"run_ref": "run-1", "reject": True})
        self.assertIn("--reject", self.seen["argv"])

    def test_settle_par_requires_confirm(self):  # #4
        r = self._call("shepherd_settle_par", {"proposal_id": "p1"})
        self.assertIn("confirm=true", r["result"]["content"][0]["text"])
        self.assertNotIn("argv", self.seen)


class ServeLoop(unittest.TestCase):
    def test_serve_reads_and_writes_jsonrpc(self):
        inp = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        )
        out = io.StringIO()
        serve(inp, out)
        lines = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
        self.assertEqual(len(lines), 2)                # initialize + tools/list; notification silent
        self.assertEqual(lines[0]["id"], 1)
        self.assertEqual(lines[1]["id"], 2)
        self.assertIn("tools", lines[1]["result"])

    def test_serve_parse_error(self):
        out = io.StringIO()
        serve(io.StringIO("not json\n"), out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
