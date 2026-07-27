"""Grok L1 host path: policy + gate + stage without Claude / shepherd-ai."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev.policy import ChangesetPolicy  # noqa: E402
from shepherd_dev.providers.grok_exec import FakeGrokExecutor  # noqa: E402
from shepherd_dev.providers.grok_host import develop_grok  # noqa: E402


class GrokHostLoop(unittest.TestCase):
    def test_fake_worker_gates_and_stages(self):
        root = Path(mkdtemp())
        (root / "seed.txt").write_text("seed\n")
        # trivial always-pass gate
        report = develop_grok(
            root,
            "add hello module",
            test_cmd="true",
            max_attempts=1,
            executor=FakeGrokExecutor({"hello.py": b"print('hi')\n"}),
            do_review=False,
        )
        self.assertTrue(report.succeeded)
        self.assertIsNotNone(report.proposal_id)
        self.assertIn("hello.py", report.entries or {})
        staged = root / ".shepherd-proposals" / report.proposal_id / "files" / "hello.py"
        self.assertTrue(staged.is_file())
        self.assertEqual(staged.read_text(), "print('hi')\n")
        manifest = json.loads(
            (root / ".shepherd-proposals" / report.proposal_id / "manifest.json").read_text()
        )
        self.assertEqual(manifest.get("provider"), "grok")
        # #3: settle-par's re-gate is the only settle-time check that a moved
        # base invalidates the proposal — hosted must carry the command too.
        self.assertEqual(manifest.get("regate_cmd"), "true")

    def test_human_edit_during_the_run_is_not_reverted(self):
        """#3: a file the human edits mid-run, untouched by the worker, must
        stay out of the proposal — otherwise settling rewrites it with the
        stale copy the clone was made from."""
        root = Path(mkdtemp())
        (root / "seed.txt").write_text("seed\n")
        (root / "human.py").write_text("original\n")

        def edit_during_run(clone: Path):
            (root / "human.py").write_text("HUMAN EDITED MID-RUN\n")

        report = develop_grok(
            root,
            "add hello module",
            test_cmd="true",
            max_attempts=1,
            executor=FakeGrokExecutor(
                {"hello.py": b"print('hi')\n"}, on_run=edit_during_run
            ),
            do_review=False,
        )
        self.assertTrue(report.succeeded)
        self.assertNotIn("human.py", report.entries or {})
        self.assertEqual(list(report.entries or {}), ["hello.py"])

    def test_policy_rejects_escape(self):
        root = Path(mkdtemp())
        (root / "seed.txt").write_text("seed\n")
        report = develop_grok(
            root,
            "evil",
            test_cmd="true",
            max_attempts=1,
            executor=FakeGrokExecutor({"../outside.py": b"x\n"}),
            policy=ChangesetPolicy(),
        )
        # materialize_into in fake writes under clone with resolve check — fake
        # executor itself blocks escape; if it wrote a nested path that escapes
        # policy, we'd see policy_rejected. With FakeGrokExecutor escape block:
        self.assertFalse(report.succeeded)

    def test_gate_fail_retries(self):
        root = Path(mkdtemp())
        (root / "seed.txt").write_text("seed\n")
        report = develop_grok(
            root,
            "x",
            test_cmd="false",
            max_attempts=2,
            executor=FakeGrokExecutor({"x.py": b"1\n"}),
        )
        self.assertFalse(report.succeeded)
        self.assertEqual(len(report.attempts), 2)
        self.assertEqual(report.attempts[0].verdict, "tests_failed")

    def test_no_claude_import_in_grok_modules(self):
        import shepherd_dev.providers.grok_exec as ge
        import shepherd_dev.providers.grok_host as gh
        import shepherd_dev.diffcollect as dc
        import inspect

        for mod in (ge, gh, dc):
            src = inspect.getsource(mod)
            self.assertNotIn("import shepherd", src)
            self.assertNotIn("from shepherd", src)


if __name__ == "__main__":
    unittest.main()
