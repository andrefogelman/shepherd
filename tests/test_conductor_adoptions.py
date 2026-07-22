"""Tests for the two conductor-study adoptions: workspace instructions
(AGENTS.md / CLAUDE.md / copilot-instructions) injected into the context pack,
and the machine-readable `run --json` report envelope.
Runnable with: python -m unittest tests.test_conductor_adoptions
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.contextpack import build_pack, workspace_instructions  # noqa: E402

try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


class WorkspaceInstructionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-wsinstr-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("APP = 1\n")

    def test_collects_known_files_in_order(self):
        (self.repo / "AGENTS.md").write_text("Always use tabs.\n")
        (self.repo / "CLAUDE.md").write_text("Never touch legacy/.\n")
        (self.repo / ".github").mkdir()
        (self.repo / ".github" / "copilot-instructions.md").write_text("Prefer small PRs.\n")
        text = workspace_instructions(self.repo)
        self.assertIn("Always use tabs.", text)
        self.assertIn("Never touch legacy/.", text)
        self.assertIn("Prefer small PRs.", text)
        self.assertLess(text.index("Always use tabs."), text.index("Never touch legacy/."))
        self.assertIn("--- AGENTS.md ---", text)

    def test_absent_files_give_empty(self):
        self.assertEqual(workspace_instructions(self.repo), "")

    def test_capped(self):
        (self.repo / "AGENTS.md").write_text("R" * 50_000)
        text = workspace_instructions(self.repo)
        self.assertLessEqual(len(text), 4_300)
        self.assertIn("truncated", text)

    def test_unreadable_is_skipped(self):
        (self.repo / "AGENTS.md").write_text("ok rules\n")
        (self.repo / "CLAUDE.md").mkdir()  # a dir with that name: skip, no crash
        text = workspace_instructions(self.repo)
        self.assertIn("ok rules", text)

    def test_pack_includes_instructions_section(self):
        (self.repo / "AGENTS.md").write_text("House rule: snake_case only.\n")
        pack, stats = build_pack(self.repo, "add feature to app")
        self.assertIn("== WORKSPACE INSTRUCTIONS", pack)
        self.assertIn("snake_case only", pack)
        self.assertTrue(stats.get("instructions"))

    def test_pack_without_instructions_has_no_section(self):
        pack, stats = build_pack(self.repo, "add feature to app")
        self.assertNotIn("== WORKSPACE INSTRUCTIONS", pack)
        self.assertFalse(stats.get("instructions"))


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class RunJsonEnvelopeTests(unittest.TestCase):
    def _report(self, succeeded=True):
        from shepherd_dev.supervisor import Attempt, DevReport, GateResult, ReviewVerdict

        report = DevReport(feature="add X", succeeded=succeeded, repo="/r")
        report.final_run_ref = "run-abc" if succeeded else None
        report.attempts = [Attempt(1, "run-abc", ["a.py"], [], GateResult(True, 0, "ok"), "passed", duration_s=3.2)]
        report.review = ReviewVerdict(approved=True, summary="fine", issues=[])
        report.entries = {"a.py": b"A = 1\n"}
        return report

    def test_envelope_shape(self):
        from shepherd_dev.cli import _report_envelope

        env = _report_envelope(self._report(), repo_root=Path("/r"), mode="feature",
                               test_cmd="pytest -q", provider="claude", verbose_run="rid-1")
        self.assertTrue(env["succeeded"])
        self.assertEqual(env["final_run_ref"], "run-abc")
        self.assertEqual(env["feature"], "add X")
        self.assertEqual(env["files"], ["a.py"])
        self.assertEqual(env["attempts"][0]["verdict"], "passed")
        self.assertTrue(env["review"]["approved"])
        self.assertEqual(env["verbose_run"], "rid-1")
        self.assertIn("settle", env)  # the settle commands, machine-consumable
        json.dumps(env)  # must be serializable

    def test_parser_run_json_flag_implies_no_settle(self):
        from shepherd_dev.cli import build_parser

        args = build_parser().parse_args(["run", "f", "--json"])
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
