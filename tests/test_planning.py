"""Tests for #4: the cheap-model planning prefetch.

Pure pieces (prompt build, response parsing across the CLI's json envelope /
code fences / hallucinated paths) are tested directly; the CLI call is tested
through a fake subprocess so nothing runs a real model. Runnable with:
    python -m unittest tests.test_planning
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import shepherd_dev.planning as PL  # noqa: E402
from shepherd_dev.planning import (  # noqa: E402
    build_plan_prompt, parse_plan_response, plan_targets,
)


class PlanPromptAndParse(unittest.TestCase):
    def test_prompt_has_feature_tree_and_json_contract(self):
        p = build_plan_prompt("add retry to charger", "svc/charger.py\nsvc/db.py")
        self.assertIn("add retry to charger", p)
        self.assertIn("svc/charger.py", p)
        self.assertIn("JSON", p)

    def test_parse_plain_json_filters_to_real_files(self):
        raw = '{"targets":["svc/charger.py","ghost.py"],"plan":"1 do x"}'
        res = parse_plan_response(raw, {"svc/charger.py", "svc/db.py"})
        self.assertEqual(res.targets, ["svc/charger.py"])   # ghost.py dropped
        self.assertEqual(res.plan, "1 do x")
        self.assertIsNone(res.error)

    def test_parse_cli_json_envelope(self):
        # `claude -p --output-format json` wraps the model text in .result
        raw = '{"type":"result","result":"{\\"targets\\":[\\"a.py\\"],\\"plan\\":\\"go\\"}"}'
        res = parse_plan_response(raw, {"a.py"})
        self.assertEqual(res.targets, ["a.py"])
        self.assertEqual(res.plan, "go")

    def test_parse_code_fenced_json(self):
        raw = '```json\n{"targets":["a.py"],"plan":"p"}\n```'
        res = parse_plan_response(raw, {"a.py"})
        self.assertEqual(res.targets, ["a.py"])

    def test_parse_garbage_sets_error(self):
        res = parse_plan_response("not json at all", {"a.py"})
        self.assertIsNotNone(res.error)
        self.assertEqual(res.targets, [])

    def test_parse_caps_targets(self):
        rels = {f"f{i}.py" for i in range(20)}
        raw = '{"targets":' + str([f"f{i}.py" for i in range(20)]).replace("'", '"') + ',"plan":""}'
        res = parse_plan_response(raw, rels)
        self.assertLessEqual(len(res.targets), PL.MAX_TARGETS)


class PlanTargetsCli(unittest.TestCase):
    def setUp(self):
        self._real_run = subprocess.run
        self._real_which = PL.shutil.which
        self._real_login = PL._host_login
        PL._host_login = lambda: None  # never read the real login in a test

    def tearDown(self):
        PL.subprocess.run = self._real_run
        PL.shutil.which = self._real_which
        PL._host_login = self._real_login

    def test_success_path(self):
        PL.shutil.which = lambda _c: "/fake/claude"
        PL.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout='{"result":"{\\"targets\\":[\\"x.py\\"],\\"plan\\":\\"s\\"}"}', stderr="")
        res = plan_targets("feat", "x.py\ny.py", {"x.py", "y.py"})
        self.assertEqual(res.targets, ["x.py"])
        self.assertEqual(res.plan, "s")

    def test_runs_isolated_from_the_repository(self):
        # Pins the planner reading a hostile repo's CLAUDE.md, hooks and
        # .mcp.json, and the user's own: it must start in an empty directory
        # that is not the caller's cwd, on a redirected config, with the
        # worker's hardening flags and no tools.
        PL.shutil.which = lambda _c: "/fake/claude"
        PL._host_login = lambda: b'{"claudeAiOauth":{}}'
        seen = {}

        def _fake(argv, **kw):
            env = kw.get("env") or {}
            cred = Path(env.get("CLAUDE_CONFIG_DIR", "/nonexistent")) / ".credentials.json"
            seen.update(argv=argv, cwd=kw.get("cwd"), env=env,
                        listing=sorted(Path(kw["cwd"]).iterdir()) if kw.get("cwd") else None,
                        config=sorted(p.name for p in cred.parent.iterdir()) if cred.parent.is_dir() else None,
                        cred=cred.read_bytes() if cred.is_file() else None,
                        cred_mode=cred.stat().st_mode & 0o777 if cred.is_file() else None)
            return subprocess.CompletedProcess(argv, 0, stdout='{"targets":["x.py"],"plan":"s"}', stderr="")

        PL.subprocess.run = _fake
        res = plan_targets("feat", "x.py", {"x.py"})
        self.assertEqual(res.targets, ["x.py"])
        self.assertIsNotNone(seen["cwd"])
        cwd = Path(seen["cwd"])
        self.assertNotEqual(cwd.resolve(), Path.cwd().resolve())
        self.assertEqual(seen["listing"], [])
        # the user's config is redirected too, into the same scratch, holding
        # nothing but the seeded login
        scratch = cwd.parent
        env = seen["env"]
        self.assertEqual(Path(env["HOME"]).parent, scratch)
        self.assertEqual(Path(env["CLAUDE_CONFIG_DIR"]).parent, scratch)
        self.assertEqual(Path(env["TMPDIR"]).parent, scratch)
        self.assertEqual(seen["config"], [".credentials.json"])
        self.assertEqual(seen["cred"], b'{"claudeAiOauth":{}}')
        self.assertEqual(seen["cred_mode"], 0o600)
        self.assertFalse(scratch.exists())  # login and scratch removed after the call
        argv = seen["argv"]
        for flag in ("--strict-mcp-config", "--no-chrome"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")
        self.assertEqual(argv[-2:], ["--tools", ""])
        # the prompt is not swallowed by the variadic --tools
        self.assertLess(argv.index(build_plan_prompt("feat", "x.py")), argv.index("--tools"))

    def test_no_host_login_seeds_nothing(self):
        # auth from the environment (or none): the redirected config stays empty
        PL.shutil.which = lambda _c: "/fake/claude"
        PL._host_login = lambda: None
        seen = {}

        def _fake(argv, **kw):
            seen["config"] = sorted(Path(kw["env"]["CLAUDE_CONFIG_DIR"]).iterdir())
            return subprocess.CompletedProcess(argv, 0, stdout='{"targets":["x.py"],"plan":"s"}', stderr="")

        PL.subprocess.run = _fake
        self.assertEqual(plan_targets("feat", "x.py", {"x.py"}).targets, ["x.py"])
        self.assertEqual(seen["config"], [])

    def test_tempdir_failure_returns_error(self):
        # the scratch is created inside the try: a full or missing TMPDIR is a
        # skipped plan, never an exception out of plan_targets
        PL.shutil.which = lambda _c: "/fake/claude"
        real = PL.tempfile.TemporaryDirectory

        def _no_tmp(*a, **k):
            raise OSError("no space left on device")

        PL.tempfile.TemporaryDirectory = _no_tmp
        try:
            res = plan_targets("feat", "x.py", {"x.py"})
        finally:
            PL.tempfile.TemporaryDirectory = real
        self.assertIn("could not run", res.error or "")
        self.assertEqual(res.targets, [])

    def test_cli_missing_returns_error(self):
        PL.shutil.which = lambda _c: None
        res = plan_targets("feat", "x.py", {"x.py"})
        self.assertIsNotNone(res.error)
        self.assertEqual(res.targets, [])

    def test_nonzero_exit_returns_error(self):
        PL.shutil.which = lambda _c: "/fake/claude"
        PL.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom")
        res = plan_targets("feat", "x.py", {"x.py"})
        self.assertIsNotNone(res.error)

    def test_timeout_returns_error(self):
        PL.shutil.which = lambda _c: "/fake/claude"

        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

        PL.subprocess.run = _boom
        res = plan_targets("feat", "x.py", {"x.py"}, timeout=1)
        self.assertIn("timed out", res.error or "")


if __name__ == "__main__":
    unittest.main()
