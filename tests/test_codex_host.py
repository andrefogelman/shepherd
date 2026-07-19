"""Codex L1 host path: policy + gate + stage + LLM review without Claude / shepherd-ai."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.policy import ChangesetPolicy  # noqa: E402
from shepherd_dev.providers.codex_exec import FakeCodexExecutor  # noqa: E402
from shepherd_dev.providers.codex_host import (  # noqa: E402
    codex_review,
    develop_codex,
)
from shepherd_dev.supervisor import ReviewVerdict  # noqa: E402


class CodexHostLoop(unittest.TestCase):
    def test_fake_worker_gates_and_stages(self):
        root = Path(tempfile.mkdtemp())
        (root / "seed.txt").write_text("seed\n")
        report = develop_codex(
            root,
            "add hello module",
            test_cmd="true",
            max_attempts=1,
            executor=FakeCodexExecutor({"hello.py": b"print('hi')\n"}),
            do_review=False,
        )
        self.assertTrue(report.succeeded)
        self.assertIsNotNone(report.proposal_id)
        self.assertIn("hello.py", report.entries or {})
        staged = root / ".shepherd-proposals" / report.proposal_id / "files" / "hello.py"
        self.assertTrue(staged.is_file())
        manifest = json.loads(
            (root / ".shepherd-proposals" / report.proposal_id / "manifest.json").read_text()
        )
        self.assertEqual(manifest.get("provider"), "codex")

    def test_policy_rejects_escape(self):
        root = Path(tempfile.mkdtemp())
        (root / "seed.txt").write_text("seed\n")
        report = develop_codex(
            root,
            "evil",
            test_cmd="true",
            max_attempts=1,
            executor=FakeCodexExecutor({"../outside.py": b"x\n"}),
            policy=ChangesetPolicy(),
        )
        self.assertFalse(report.succeeded)

    def test_gate_fail_retries(self):
        root = Path(tempfile.mkdtemp())
        (root / "seed.txt").write_text("seed\n")
        report = develop_codex(
            root,
            "x",
            test_cmd="false",
            max_attempts=2,
            executor=FakeCodexExecutor({"x.py": b"1\n"}),
        )
        self.assertFalse(report.succeeded)
        self.assertEqual(len(report.attempts), 2)
        self.assertEqual(report.attempts[0].verdict, "tests_failed")

    def test_review_fn_injected(self):
        """do_review=True routes through the codex reviewer (injected here)."""
        root = Path(tempfile.mkdtemp())
        (root / "seed.txt").write_text("seed\n")
        seen: dict = {}

        def fake_review(clone, entries, feature):
            seen["files"] = sorted(entries)
            return ReviewVerdict(True, "looks good", [])

        report = develop_codex(
            root,
            "reviewed feature",
            test_cmd="true",
            max_attempts=1,
            executor=FakeCodexExecutor({"a.py": b"x\n"}),
            do_review=True,
            review_fn=fake_review,
        )
        self.assertTrue(report.succeeded)
        self.assertIsNotNone(report.review)
        self.assertTrue(report.review.approved)
        self.assertEqual(seen["files"], ["a.py"])
        manifest = json.loads(
            (root / ".shepherd-proposals" / report.proposal_id / "manifest.json").read_text()
        )
        self.assertTrue(manifest["review"]["approved"])

    def test_summary_names_codex(self):
        root = Path(tempfile.mkdtemp())
        (root / "seed.txt").write_text("seed\n")
        report = develop_codex(
            root,
            "x",
            test_cmd="true",
            max_attempts=1,
            executor=FakeCodexExecutor({"y.py": b"1\n"}),
        )
        self.assertIn("provider: codex", report.summary())
        self.assertIn("settle-par", report.summary())

    def test_no_shepherd_import_in_codex_modules(self):
        import inspect

        import shepherd_dev.providers.codex_exec as ce
        import shepherd_dev.providers.codex_host as ch
        import shepherd_dev.providers.hosted as hd

        for mod in (ce, ch, hd):
            src = inspect.getsource(mod)
            self.assertNotIn("import shepherd\n", src)
            self.assertNotIn("from shepherd import", src)
            self.assertNotIn("import shepherd as", src)


class CodexReviewParsing(unittest.TestCase):
    def _entries(self):
        return {"a.py": b"print(1)\n"}

    def test_parses_json_verdict(self):
        def runner(argv, *, timeout, last_message_path):
            Path(last_message_path).write_text(
                json.dumps({"approved": True, "summary": "ok", "issues": []})
            )
            return 0, "done"

        v = codex_review(
            Path(tempfile.mkdtemp()), self._entries(), "f",
            codex_bin="/bin/codex", runner=runner,
        )
        self.assertTrue(v.approved)
        self.assertEqual(v.summary, "ok")
        self.assertIsNone(v.error)

    def test_rejected_with_issues(self):
        def runner(argv, *, timeout, last_message_path):
            Path(last_message_path).write_text(
                json.dumps({"approved": False, "summary": "bad", "issues": ["no tests"]})
            )
            return 0, "done"

        v = codex_review(
            Path(tempfile.mkdtemp()), self._entries(), "f",
            codex_bin="/bin/codex", runner=runner,
        )
        self.assertFalse(v.approved)
        self.assertEqual(v.issues, ["no tests"])

    def test_extracts_json_from_prose(self):
        def runner(argv, *, timeout, last_message_path):
            Path(last_message_path).write_text(
                'Here is my verdict:\n{"approved": true, "summary": "fine", "issues": []}\nthanks'
            )
            return 0, "done"

        v = codex_review(
            Path(tempfile.mkdtemp()), self._entries(), "f",
            codex_bin="/bin/codex", runner=runner,
        )
        self.assertTrue(v.approved)

    def test_failure_becomes_error_verdict(self):
        def runner(argv, *, timeout, last_message_path):
            return 1, "exploded"

        v = codex_review(
            Path(tempfile.mkdtemp()), self._entries(), "f",
            codex_bin="/bin/codex", runner=runner,
        )
        self.assertFalse(v.approved)
        self.assertIsNotNone(v.error)

    def test_no_binary_is_error_verdict(self):
        v = codex_review(
            Path(tempfile.mkdtemp()), self._entries(), "f", codex_bin=None,
        )
        self.assertFalse(v.approved)
        self.assertIsNotNone(v.error)


if __name__ == "__main__":
    unittest.main()
