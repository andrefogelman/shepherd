"""Tests for the verbose renderer + supervisor event emission + trace replay
(Fase 4 of verbose mode). Runnable with: python -m unittest tests.test_verbose
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.events import RunEventLog, repo_baseline_reader  # noqa: E402
from shepherd_dev.progress import VerboseReporter, format_event, render_trace  # noqa: E402
from shepherd_dev.supervisor import develop  # noqa: E402


def _ev(kind: str, payload: dict | None = None, attempt: int | None = None, ts: float = 100.0, seq: int = 1):
    event: dict = {"ts": ts, "seq": seq, "kind": kind}
    if attempt is not None:
        event["attempt"] = attempt
    if payload:
        event["payload"] = payload
    return event


class FormatEventTests(unittest.TestCase):
    def test_worker_kinds(self):
        self.assertIn("Edit", format_event(_ev("worker.tool", {"tool": "Edit", "target": "src/a.py"})))
        s = format_event(_ev("worker.edit", {"path": "/jail/ws/src/a.py", "add": 2, "del": 1, "hunk": ""}))
        self.assertIn("src/a.py", s)
        self.assertIn("+2", s)
        self.assertIn("−1", s)
        self.assertNotIn("/jail/", s)  # jail-absolute paths are shortened
        s = format_event(_ev("worker.write", {"path": "src/b.py", "lines": 4, "bytes": 40}))
        self.assertIn("src/b.py", s)
        self.assertIn("4", s)
        self.assertIn("boom", format_event(_ev("worker.tool.fail", {"error": "boom"})))

    def test_gate_and_review_kinds(self):
        s = format_event(_ev("gate.test.fail", {"framework": "pytest", "test": "t/x.py::t_a"}))
        self.assertIn("t/x.py::t_a", s)
        self.assertIn("pytest", s)
        self.assertIn("failed", format_event(_ev("gate.result", {"passed": False, "exit_code": 1})))
        self.assertIn("passed", format_event(_ev("gate.result", {"passed": True, "exit_code": 0})))
        self.assertIn("REJECTED", format_event(_ev("review.verdict", {"approved": False})))
        self.assertIn("policy", format_event(_ev("policy.reject", {"violations": ["a", "b"]})))

    def test_phase_events_hidden_live_but_shown_in_trace(self):
        e = _ev("phase.start", {"label": "worker"}, attempt=1)
        self.assertIsNone(format_event(e, live=True))
        self.assertIn("worker", format_event(e, live=False))

    def test_gate_line(self):
        e = _ev("gate.line", {"line": "collected 3 items"})
        self.assertIn("collected 3 items", format_event(e, live=True))

    def test_parallel_kinds(self):
        s = format_event(_ev("parallel.conflicts", {"files": ["index.html"], "handoff": True}))
        self.assertIn("index.html", s)
        self.assertIn("handoff", s)
        self.assertIn("no conflicts", format_event(_ev("parallel.conflicts", {"files": [], "handoff": False})))
        self.assertIn("round 2", format_event(_ev("parallel.repair", {"round": 2, "exit_code": 1})))

    def test_unknown_kind_is_none(self):
        self.assertIsNone(format_event(_ev("something.else", {})))


class VerboseReporterTests(unittest.TestCase):
    def test_events_render_as_notes(self):
        buf = io.StringIO()
        rep = VerboseReporter(stream=buf, enabled=False)
        rep.step("attempt 1/3 · worker running")
        rep.handle_event(_ev("worker.edit", {"path": "src/a.py", "add": 1, "del": 0, "hunk": ""}))
        rep.handle_event(_ev("phase.start", {"label": "worker"}))  # hidden live
        rep.close()
        out = buf.getvalue()
        self.assertIn("src/a.py", out)
        self.assertIn("+1", out)
        self.assertNotIn("▶ worker\n", out.replace("▶ attempt", ""))  # no phase dup


class RenderTraceTests(unittest.TestCase):
    EVENTS = [
        _ev("phase.start", {"label": "worker"}, attempt=1, ts=100.0, seq=1),
        _ev("worker.edit", {"path": "a.py", "add": 1, "del": 0, "hunk": ""}, attempt=1, ts=101.5, seq=2),
        _ev("gate.line", {"line": "collected 1 item"}, attempt=1, ts=103.0, seq=3),
        _ev("gate.test.fail", {"framework": "pytest", "test": "t.py::a"}, attempt=1, ts=104.0, seq=4),
        _ev("run.summary", {"succeeded": False, "attempts": 1, "final_run_ref": None}, ts=105.0, seq=5),
    ]

    def test_default_hides_gate_lines_shows_failures(self):
        lines = render_trace(self.EVENTS)
        text = "\n".join(lines)
        self.assertNotIn("collected 1 item", text)
        self.assertIn("t.py::a", text)
        self.assertIn("worker", text)  # phase events shown post-hoc
        self.assertIn("+0.0s", lines[0])
        self.assertIn("+1.5s", lines[1])

    def test_full_includes_gate_lines(self):
        text = "\n".join(render_trace(self.EVENTS, full=True))
        self.assertIn("collected 1 item", text)


class BaselineReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-base-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("V = 1\n")

    def test_jail_absolute_path_resolves_by_suffix(self):
        read = repo_baseline_reader(self.repo)
        self.assertEqual(read("/jail/fork-xyz/src/a.py"), "V = 1\n")
        self.assertEqual(read("src/a.py"), "V = 1\n")

    def test_missing_and_traversal_paths_return_none(self):
        read = repo_baseline_reader(self.repo)
        self.assertIsNone(read("/jail/fork/src/nope.py"))
        self.assertIsNone(read("../../etc/passwd"))
        self.assertIsNone(read(""))


# --- minimal fake workspace (same shape as test_supervisor) -------------------
class _Changeset:
    def __init__(self, files):
        self._files = files

    @property
    def changed_paths(self):
        return list(self._files)

    def read_file(self, rel):
        b = self._files.get(rel)
        return (b, 0o644) if b is not None else None


class _Output:
    def __init__(self, cs):
        self._cs = cs

    def changeset(self):
        return self._cs

    def discard(self):
        pass


class _Run:
    def __init__(self, ref, cs):
        self.run_ref = ref
        self._out = _Output(cs)

    def output(self):
        return self._out


class _Tasks:
    def register(self, task):
        pass


class _Workspace:
    def __init__(self, proposals):
        self._proposals = proposals
        self._i = 0
        self.tasks = _Tasks()

    def run(self, task, *, placement=None, runtime=None, **args):
        cs = _Changeset(self._proposals[min(self._i, len(self._proposals) - 1)])
        self._i += 1
        return _Run(f"r{self._i}", cs)

    def git_repo(self):
        return None


class DevelopEmitsEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-dev-ev-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "seed.txt").write_text("seed\n")
        self.log = RunEventLog(run_id="dev", root=self.repo / "runs")
        self.seen: list[dict] = []
        self.log.subscribe(self.seen.append)

    def kinds(self):
        return [e["kind"] for e in self.seen]

    def test_failing_gate_emits_diff_gate_and_failure_events(self):
        ws = _Workspace([{"impl.py": b"X = 1\n"}])
        report = develop(
            ws, None, repo=None, repo_root=self.repo, feature="f",
            test_cmd='echo "FAILED t/x.py::t_a - boom"; exit 1',
            provider="static", placement="advisory", max_attempts=1,
            event_log=self.log,
        )
        self.assertFalse(report.succeeded)
        kinds = self.kinds()
        self.assertIn("phase.start", kinds)
        self.assertIn("attempt.diff", kinds)
        self.assertIn("gate.line", kinds)
        self.assertIn("gate.test.fail", kinds)
        self.assertIn("gate.result", kinds)
        fails = [e for e in self.seen if e["kind"] == "gate.test.fail"]
        self.assertEqual(fails[0]["payload"]["test"], "t/x.py::t_a")
        results = [e for e in self.seen if e["kind"] == "gate.result"]
        self.assertFalse(results[0]["payload"]["passed"])

    def test_passing_gate_emits_passed_result(self):
        ws = _Workspace([{"impl.py": b"X = 1\n"}])
        report = develop(
            ws, None, repo=None, repo_root=self.repo, feature="f",
            test_cmd="echo ok", provider="static", placement="advisory",
            max_attempts=1, event_log=self.log,
        )
        self.assertTrue(report.succeeded)
        results = [e for e in self.seen if e["kind"] == "gate.result"]
        self.assertTrue(results[0]["payload"]["passed"])
        diffs = [e for e in self.seen if e["kind"] == "attempt.diff"]
        self.assertEqual(diffs[0]["payload"]["files"], ["impl.py"])

    def test_policy_rejection_emits_policy_event(self):
        from shepherd_dev.policy import ChangesetPolicy

        ws = _Workspace([{".env": b"SECRET=1\n"}])
        develop(
            ws, None, repo=None, repo_root=self.repo, feature="f",
            test_cmd="echo ok", provider="static", placement="advisory",
            max_attempts=1, policy=ChangesetPolicy(), event_log=self.log,
        )
        self.assertIn("policy.reject", self.kinds())


class ThreadBoundHookTests(unittest.TestCase):
    """Parallel best-of: one global hook, per-thread candidate logs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-tbh-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_set_attempt_updates_the_slot_the_thread_reads(self):
        import threading

        from shepherd_dev.events import WorkerStreamHook

        log_main = RunEventLog(run_id="m", root=self.root / "runs")
        hook = WorkerStreamHook(log_main)
        hook.set_attempt(3)  # unbound thread: shared slot
        self.assertEqual(hook._current(), (log_main, 3))

        seen: list[tuple] = []

        def bound():
            log_t = RunEventLog(run_id="t", root=self.root / "runs")
            hook.bind(log_t)
            hook.set_attempt(2)  # bound thread: MUST hit the thread-local slot
            seen.append(hook._current())

        t = threading.Thread(target=bound)
        t.start()
        t.join(5)
        self.assertEqual(seen[0][1], 2)
        self.assertEqual(hook._current(), (log_main, 3))  # main thread untouched

    def test_bound_thread_routes_to_its_log_unbound_gets_none(self):
        import threading

        from shepherd_dev.events import WorkerStreamHook

        hook = WorkerStreamHook()  # no default log
        self.assertIsNone(hook.start(self.root / "wsX"))  # unbound + no default

        logs = {name: RunEventLog(run_id=name, root=self.root / "runs") for name in ("a", "b")}
        results: dict[str, list[str]] = {}

        def worker(name: str):
            hook.bind(logs[name])
            ws = self.root / f"ws-{name}"
            tee = hook.tee_path(ws)
            tee.parent.mkdir(parents=True, exist_ok=True)
            tailer = hook.start(ws)
            tee.write_text(
                '{"type":"assistant","message":{"content":[{"type":"text","text":"from %s"}]}}\n' % name
            )
            hook.drain(tailer)
            results[name] = [
                e["payload"]["text"]
                for e in map(__import__("json").loads, logs[name].path.read_text().splitlines())
                if e["kind"] == "worker.note"
            ]

        threads = [__import__("threading").Thread(target=worker, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(results["a"], ["from a"])
        self.assertEqual(results["b"], ["from b"])
        del threading


try:  # the CLI imports the substrate; skip its tests where it is absent
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class VerboseDefaultTests(unittest.TestCase):
    """Verbose is the DEFAULT run mode; --no-verbose is the opt-out toggle."""

    def _parse(self, argv):
        from shepherd_dev.cli import build_parser

        return build_parser().parse_args(argv)

    def test_run_defaults_to_verbose(self):
        self.assertTrue(self._parse(["run", "feat"]).verbose)

    def test_no_verbose_toggle(self):
        self.assertFalse(self._parse(["run", "feat", "--no-verbose"]).verbose)

    def test_v_flag_still_accepted(self):
        self.assertTrue(self._parse(["run", "feat", "-v"]).verbose)

    def test_run2_defaults_to_verbose_with_toggle(self):
        self.assertTrue(self._parse(["run2", "a", "b"]).verbose)
        self.assertFalse(self._parse(["run2", "a", "b", "--no-verbose"]).verbose)


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class CmdTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-trace-")
        self.addCleanup(self.tmp.cleanup)
        import os

        os.environ["SHEPHERD_DEV_RUNS_DIR"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SHEPHERD_DEV_RUNS_DIR", None)
        log = RunEventLog(run_id="20260718-000000-abcdef", root=Path(self.tmp.name))
        log.emit("phase.start", {"label": "worker"}, attempt=1)
        log.emit("gate.test.fail", {"framework": "pytest", "test": "t.py::a"}, attempt=1)

    def _trace(self, **kw):
        import contextlib
        from types import SimpleNamespace

        from shepherd_dev.cli import cmd_trace

        defaults = {"run_id": "last", "full": False, "json": False}
        defaults.update(kw)
        args = SimpleNamespace(**defaults)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cmd_trace(args)
        return code, buf.getvalue()

    def test_trace_last_renders_timeline(self):
        code, out = self._trace()
        self.assertEqual(code, 0)
        self.assertIn("t.py::a", out)
        self.assertIn("worker", out)

    def test_trace_json_outputs_ndjson(self):
        code, out = self._trace(json=True)
        self.assertEqual(code, 0)
        first = out.splitlines()[0]
        import json as _json

        self.assertEqual(_json.loads(first)["kind"], "phase.start")


if __name__ == "__main__":
    unittest.main()
