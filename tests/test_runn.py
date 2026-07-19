"""Tests for runN — up to 5 INDEPENDENT features in parallel lanes, each with
its own gate/review/staged proposal — and its two methodology guardrails:
the settle-time re-gate (a proposal built on a stale base must re-pass the
suite against the REAL post-settle worktree before writing) and the overlap
warning. Runnable with: python -m unittest tests.test_runn
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class DevelopManyTests(unittest.TestCase):
    """develop_many with stubbed lanes (no substrate workers)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-runn-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("A = 1\n")

    def _develop_many(self, features, lane_entries, test_cmd="echo ok", **kw):
        """Run develop_many with _run_lane stubbed to canned per-feature entries."""
        from shepherd_dev import parallel as P

        def fake_lane(clone, feature, *, test_cmd, gate_lock, **_kw):
            from shepherd_dev.supervisor import DevReport, GateResult

            i = features.index(feature)
            entries = lane_entries[i]
            report = DevReport(feature=feature, succeeded=bool(entries), repo=str(clone))
            report.entries = entries or None
            report.final_run_ref = f"run-lane{i}"
            if entries:
                report.gate = GateResult(True, 0, "ok")  # type: ignore[attr-defined]
            return report

        old_lane, old_clone = P._run_lane, P._clone_workspace
        P._run_lane = fake_lane
        P._clone_workspace = lambda repo_root, overlay=None: self.repo
        try:
            return P.develop_many(
                self.repo, list(features), test_cmd=test_cmd, provider="static", **kw
            )
        finally:
            P._run_lane, P._clone_workspace = old_lane, old_clone

    def test_each_lane_stages_its_own_proposal_with_regate_cmd(self):
        report = self._develop_many(
            ["feat A", "feat B"],
            [{"src/a.py": b"A = 2\n"}, {"src/b.py": b"B = 1\n"}],
        )
        self.assertTrue(report.succeeded)
        ids = [lane.proposal_id for lane in report.lanes]
        self.assertTrue(all(ids))
        self.assertEqual(len(set(ids)), 2)
        for lane in report.lanes:
            manifest = json.loads(
                (self.repo / ".shepherd-proposals" / lane.proposal_id / "manifest.json").read_text()
            )
            self.assertEqual(manifest["regate_cmd"], "echo ok")  # settle guardrail armed
            self.assertIn("feature", manifest)

    def test_one_failed_lane_does_not_sink_the_others(self):
        report = self._develop_many(
            ["ok one", "broken", "ok two"],
            [{"src/x.py": b"X = 1\n"}, {}, {"src/y.py": b"Y = 1\n"}],
        )
        self.assertFalse(report.lanes[1].succeeded)
        self.assertTrue(report.lanes[0].succeeded)
        self.assertTrue(report.lanes[2].succeeded)
        self.assertIsNone(report.lanes[1].proposal_id)
        self.assertTrue(report.succeeded)  # partial success is success

    def test_overlap_between_proposals_is_reported(self):
        report = self._develop_many(
            ["feat A", "feat B"],
            [{"src/shared.py": b"A\n"}, {"src/shared.py": b"B\n", "src/b.py": b"B\n"}],
        )
        self.assertIn("src/shared.py", report.conflicts)
        self.assertIn("src/shared.py", report.summary())

    def test_feature_count_is_clamped_2_to_5(self):
        from shepherd_dev.parallel import develop_many

        with self.assertRaises(AssertionError):
            develop_many(self.repo, ["only one"], test_cmd="echo ok")
        with self.assertRaises(AssertionError):
            develop_many(self.repo, [f"f{i}" for i in range(6)], test_cmd="echo ok")

    def test_lanes_run_concurrently_but_gates_serialize(self):
        import threading
        import time

        from shepherd_dev import parallel as P

        active = {"workers": 0, "max_workers": 0, "gates": 0, "max_gates": 0}
        lock = threading.Lock()

        def fake_lane(clone, feature, *, test_cmd, gate_lock, **_kw):
            from shepherd_dev.supervisor import DevReport

            with lock:
                active["workers"] += 1
                active["max_workers"] = max(active["max_workers"], active["workers"])
            time.sleep(0.15)  # "worker" phase — should overlap
            with gate_lock:  # "gate" phase — must serialize
                with lock:
                    active["gates"] += 1
                    active["max_gates"] = max(active["max_gates"], active["gates"])
                time.sleep(0.05)
                with lock:
                    active["gates"] -= 1
            with lock:
                active["workers"] -= 1
            report = DevReport(feature=feature, succeeded=True, repo=str(clone))
            report.entries = {f"f{feature[-1]}.py": b"x\n"}
            report.final_run_ref = "run-x"
            return report

        old_lane, old_clone = P._run_lane, P._clone_workspace
        P._run_lane = fake_lane
        P._clone_workspace = lambda repo_root, overlay=None: self.repo
        try:
            P.develop_many(self.repo, ["fa", "fb", "fc"], test_cmd="echo ok",
                           provider="static", max_workers=3)
        finally:
            P._run_lane, P._clone_workspace = old_lane, old_clone
        self.assertGreaterEqual(active["max_workers"], 2)  # lanes overlapped
        self.assertEqual(active["max_gates"], 1)           # gates never did


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class SettleRegateTests(unittest.TestCase):
    """The settle-time re-gate guardrail: a staged proposal whose manifest
    carries regate_cmd only writes files after the suite passes against the
    REAL current worktree + the proposal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-regate-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "base.py").write_text("BASE = 1\n")

    def _stage(self, entries: dict[str, bytes], regate_cmd: str | None):
        from shepherd_dev.staging import stage_proposal

        extra: dict = {"feature": "f"}
        if regate_cmd is not None:
            extra["regate_cmd"] = regate_cmd
        pid, _ = stage_proposal(self.repo, entries, extra)
        return pid

    def _settle(self, pid, reject=False):
        from shepherd_dev.cli import settle_proposal

        return settle_proposal(self.repo, pid, reject=reject)

    def test_regate_pass_writes_files(self):
        pid = self._stage(
            {"src/new.py": b"N = 1\n"},
            'python3 -c "import pathlib; assert pathlib.Path(\'src/new.py\').exists()"',
        )
        code, written = self._settle(pid)
        self.assertEqual(code, 0)
        self.assertEqual(written, ["src/new.py"])
        self.assertTrue((self.repo / "src" / "new.py").exists())

    def test_regate_fail_refuses_and_keeps_proposal(self):
        pid = self._stage({"src/new.py": b"N = 1\n"}, "exit 1")
        code, written = self._settle(pid)
        self.assertNotEqual(code, 0)
        self.assertEqual(written, [])
        self.assertFalse((self.repo / "src" / "new.py").exists())  # nothing written
        # proposal stays staged for a re-run decision
        self.assertTrue((self.repo / ".shepherd-proposals" / pid).is_dir())

    def test_regate_judges_the_post_settle_reality(self):
        # The gate sees current worktree + proposal — a base change AFTER the
        # proposal was built (another settle) is what the re-gate exists to catch.
        pid = self._stage(
            {"src/new.py": b"import sys\nsys.path.insert(0, 'src')\nfrom base import BASE\n"},
            'python3 -c "exec(open(\'src/base.py\').read()); assert BASE == 1"',
        )
        (self.repo / "src" / "base.py").write_text("BASE = 2\n")  # base drifted
        code, _ = self._settle(pid)
        self.assertNotEqual(code, 0)

    def test_no_regate_cmd_settles_as_before(self):
        pid = self._stage({"src/new.py": b"N = 1\n"}, None)
        code, written = self._settle(pid)
        self.assertEqual(code, 0)
        self.assertEqual(written, ["src/new.py"])

    def test_reject_skips_the_regate(self):
        pid = self._stage({"src/new.py": b"N = 1\n"}, "exit 1")
        code, written = self._settle(pid, reject=True)
        self.assertEqual(code, 0)
        self.assertEqual(written, [])


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class RunNParserTests(unittest.TestCase):
    def _parse(self, argv):
        from shepherd_dev.cli import build_parser

        return build_parser().parse_args(argv)

    def test_runn_parses_features_and_defaults(self):
        args = self._parse(["runN", "a", "b", "c"])
        self.assertEqual(args.features, ["a", "b", "c"])
        self.assertEqual(args.max_workers, 3)
        self.assertTrue(args.verbose)

    def test_max_workers_flag(self):
        self.assertEqual(self._parse(["runN", "a", "b", "--max-workers", "5"]).max_workers, 5)


class McpRunNTests(unittest.TestCase):
    def test_tool_argv(self):
        from shepherd_dev.mcpserver import _argv_for

        argv = _argv_for("shepherd_runN", {
            "features": ["a", "b", "c"], "repo": "/x", "max_workers": 2,
        })
        self.assertEqual(argv[0], "runN")
        self.assertEqual(argv[1:4], ["a", "b", "c"])
        self.assertIn("--no-verbose", argv)
        self.assertIn("--max-workers", argv)

    def test_features_count_validated(self):
        from shepherd_dev.mcpserver import _argv_for

        for bad in (["only"], [f"f{i}" for i in range(6)]):
            with self.assertRaises(ValueError):
                _argv_for("shepherd_runN", {"features": bad})


if __name__ == "__main__":
    unittest.main()
