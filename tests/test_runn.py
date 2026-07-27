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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from clonestub import clone_stub  # noqa: E402

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
        P._clone_workspace = clone_stub(self.repo)
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
        P._clone_workspace = clone_stub(self.repo)
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

    def test_partial_write_failure_keeps_the_proposal_staged(self):
        """#6: settle_run dumps its consumed content to a recovery dir when the
        worktree write fails; settle_par had no such path. Its content is still
        on disk, so the equivalent guarantee is: do NOT delete the stage, and
        say which files landed before the failure."""
        pid = self._stage(
            {"src/aaa.py": b"A = 1\n", "src/blocked.py": b"B = 1\n"}, None
        )
        # a directory where a file must go: write_bytes raises IsADirectoryError
        (self.repo / "src" / "blocked.py").mkdir()

        code, written = self._settle(pid)
        self.assertEqual(code, 2)
        self.assertEqual(written, [])
        # the stage survives, so nothing is lost and settling can be retried
        self.assertTrue((self.repo / ".shepherd-proposals" / pid / "files").is_dir())
        # ...and the file that DID land is really there
        self.assertTrue((self.repo / "src" / "aaa.py").is_file())

    def test_partial_write_is_recorded_with_what_landed(self):
        from shepherd_dev import history

        pid = self._stage(
            {"src/aaa.py": b"A = 1\n", "src/blocked.py": b"B = 1\n"}, None
        )
        (self.repo / "src" / "blocked.py").mkdir()
        events: list[tuple] = []
        old = history.record_event
        history.record_event = lambda kind, payload: events.append((kind, payload))
        try:
            self._settle(pid)
        finally:
            history.record_event = old
        actions = [p for k, p in events if p.get("action") == "accept_failed"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["written"], ["src/aaa.py"])
        self.assertIn("error", actions[0])


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class RunNPackPipelining(unittest.TestCase):
    """A2: cmd_runN built its N packs serially, each with its own planning
    subprocess and two full repo scans. The scan does not depend on the
    feature; the planning calls are independent and network-bound."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-runn-pack-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("A = 1\n")
        # named so keyword scoring puts a different file in each feature's pack
        (self.repo / "src" / "alpha.py").write_text("ALPHA = 1\n")
        (self.repo / "src" / "beta.py").write_text("BETA = 1\n")

    def _args(self, features):
        import argparse

        return argparse.Namespace(
            features=list(features), repo=str(self.repo), test_cmd="true",
            provider="claude", allowed_prefix=[], no_context_pack=False,
            no_plan=False, max_attempts=1, gate_timeout=600, worker_budget=900,
            max_changed_paths=40, max_workers=len(features), no_review=True,
            verbose=False, json=False, notify=False,
        )

    def _run(self, features, plan_delay=0.3):
        import threading
        import time

        from shepherd_dev import cli as C

        marks = {"plans_now": 0, "plans_max": 0, "scans": 0}
        stamps: list[float] = []
        lock = threading.Lock()

        def fake_planning(args, repo_root, feature_text, scan=None, out=None):
            with lock:
                marks["plans_now"] += 1
                marks["plans_max"] = max(marks["plans_max"], marks["plans_now"])
                stamps.append(time.monotonic())
            try:
                time.sleep(plan_delay)
                return (), ""
            finally:
                with lock:
                    marks["plans_now"] -= 1
                    stamps.append(time.monotonic())

        from shepherd_dev import contextpack as CP

        real_iter = CP._iter_files

        def counting_iter(repo_root, allowed_prefixes):
            with lock:
                marks["scans"] += 1
            return real_iter(repo_root, allowed_prefixes)

        old = (C._run_planning, CP._iter_files, C._resolve_repo, C._resolve_gate)
        C._run_planning = fake_planning
        CP._iter_files = counting_iter
        C._resolve_repo = lambda repo: self.repo
        C._resolve_gate = lambda root, cmd, provider: ("true", None, True)

        from shepherd_dev import parallel as P

        old_many = P.develop_many
        captured = {}

        def fake_many(repo_root, features_, **kw):
            captured["packs"] = kw.get("context_packs")
            from shepherd_dev.parallel import ManyReport

            report = ManyReport(features=list(features_))
            report.succeeded = True
            return report

        P.develop_many = fake_many
        try:
            C.cmd_runN(self._args(features))
        finally:
            C._run_planning, CP._iter_files, C._resolve_repo, C._resolve_gate = old
            P.develop_many = old_many
        # the pack PHASE only — cmd_runN does plenty around it
        pack_phase = (max(stamps) - min(stamps)) if stamps else 0.0
        return marks, pack_phase, captured

    def test_planning_runs_concurrently_across_features(self):
        marks, _elapsed, _cap = self._run(["feat a", "feat b", "feat c"])
        self.assertGreater(marks["plans_max"], 1, "planning must overlap across lanes")

    def test_the_repo_is_scanned_once_for_all_features(self):
        marks, _elapsed, _cap = self._run(["feat a", "feat b", "feat c"])
        self.assertEqual(marks["scans"], 1)

    def test_pack_phase_beats_the_serial_sum(self):
        features = ["feat a", "feat b", "feat c"]
        delay = 0.3
        _marks, pack_phase, _cap = self._run(features, plan_delay=delay)
        self.assertLess(pack_phase, len(features) * delay * 0.8,
                        f"{pack_phase:.2f}s vs serial {len(features) * delay:.2f}s")

    def test_a_failed_pack_lane_says_the_overlap_check_is_incomplete(self):
        """The guardrail compares PLANNED targets across features. A feature
        whose planning failed contributes none, so it cannot be found to
        overlap — and silence from it read as 'independent'."""
        import contextlib
        import io

        from shepherd_dev import cli as C

        calls = {"n": 0}

        def flaky_planning(args, repo_root, feature_text, scan=None, out=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("planner unavailable")
            return (), ""

        err = io.StringIO()
        old = (C._run_planning, C._resolve_repo, C._resolve_gate)
        C._run_planning = flaky_planning
        C._resolve_repo = lambda repo: self.repo
        C._resolve_gate = lambda root, cmd, provider: ("true", None, True)

        from shepherd_dev import parallel as P

        old_many = P.develop_many

        def fake_many(repo_root, features_, **kw):
            from shepherd_dev.parallel import ManyReport

            r = ManyReport(features=list(features_))
            r.succeeded = True
            return r

        P.develop_many = fake_many
        try:
            with contextlib.redirect_stderr(err):
                C.cmd_runN(self._args(["alpha thing", "beta thing", "gamma thing"]))
        finally:
            C._run_planning, C._resolve_repo, C._resolve_gate = old
            P.develop_many = old_many

        text = err.getvalue()
        self.assertIn("INCOMPLETE", text)
        self.assertIn("not evidence", text)   # names what silence does NOT mean
        self.assertIn("planning failed for feature", text)

    def test_every_feature_still_gets_its_own_pack_in_order(self):
        _marks, _phase, captured = self._run(["alpha module", "beta module"])
        packs = captured["packs"]
        self.assertEqual(len(packs), 2)
        self.assertIn("src/alpha.py", packs[0])
        self.assertNotIn("== FILE: src/beta.py", packs[0])
        self.assertIn("src/beta.py", packs[1])
        self.assertNotIn("== FILE: src/alpha.py", packs[1])


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
