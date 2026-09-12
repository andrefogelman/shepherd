"""Performance regressions pinned by work/memory bounds, not stopwatch races.

Run remotely with: python -m unittest discover -s tests -p test_io_perf.py -v
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev import contextpack as CP  # noqa: E402
from shepherd_dev import diffcollect as DC  # noqa: E402
from shepherd_dev import events as E  # noqa: E402
from shepherd_dev import history as H  # noqa: E402
from shepherd_dev import supervisor as S  # noqa: E402
from shepherd_dev import status as ST  # noqa: E402
from shepherd_dev.procstream import run_streaming  # noqa: E402


class ScratchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shepherd-io-perf-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def put(self, rel, data=b"x = 1\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class PrunedTraversal(ScratchTest):
    def test_neither_scan_descends_into_ignored_trees(self):
        self.put("src/a.py")
        for name in ("node_modules", ".git", ".venv"):
            self.put(f"src/{name}/nested/noise.py")
        real = os.scandir
        visited = []

        def guarded(path):
            visited.append(Path(path))
            self.assertFalse(set(Path(path).parts) & {"node_modules", ".git", ".venv"})
            return real(path)

        with patch("os.scandir", side_effect=guarded):
            self.assertEqual(CP._iter_files(self.root, ()), [self.root / "src/a.py"])
            self.assertEqual(set(DC.snapshot_tree(self.root)), {"src/a.py"})
        self.assertTrue(visited)

    def test_context_order_cap_and_prefix_match_sorted_path_order(self):
        for rel in ("a/z.py", "a.py", "a-/b.py", "z/last.py", "a/inner/a.py"):
            self.put(rel)
        expected = sorted(self.root.rglob("*.py"))
        self.assertEqual(CP._iter_files(self.root, ()), expected)
        self.assertEqual(CP._iter_files(self.root, ("a/",)),
                         [p for p in expected if p.relative_to(self.root).as_posix().startswith("a/")])
        real = os.scandir

        def guarded(path):
            self.assertNotEqual(Path(path), self.root / "z", "scan continued past its cap")
            return real(path)

        with patch.object(CP, "SCAN_FILE_CAP", 2), patch("os.scandir", side_effect=guarded):
            self.assertEqual(CP._iter_files(self.root, ()), expected[:2])

    def test_context_keeps_github_and_skips_other_hidden_directories(self):
        self.put(".github/workflows/ci.yml")
        self.put(".hidden/noise.py")
        self.put("src/.hidden/noise.py")
        self.assertEqual(CP._iter_files(self.root, ()), [self.root / ".github/workflows/ci.yml"])

    def test_diff_skips_file_and_directory_symlinks_and_named_noise(self):
        target = self.put("src/a.py")
        (self.root / "link.py").symlink_to(target)
        (self.root / "loop").symlink_to(self.root, target_is_directory=True)
        self.put("src/.git", b"gitdir: /elsewhere")
        self.assertEqual(set(DC.snapshot_tree(self.root)), {"src/a.py"})

    def test_empty_ignore_set_includes_nested_noise(self):
        self.put("node_modules/a.py")
        self.assertEqual(set(DC.snapshot_tree(self.root, ignore_dirs=set())), {"node_modules/a.py"})


class BoundedReads(ScratchTest):
    def test_scoring_and_import_reads_request_only_their_byte_budget(self):
        path = self.root / "a.py"
        requests = []

        class Reader(io.BytesIO):
            def read(self, size=-1):
                requests.append(size)
                if size < 0:
                    raise AssertionError("unbounded read")
                return super().read(size)

        data = b"from helper import h\n" + b"x" * (CP.READ_CAP + 100)
        with patch.object(Path, "open", side_effect=lambda *a, **kw: Reader(data)):
            text = CP._read_text(path)
            edges = CP._import_edges([path], self.root, {"a.py", "helper.py"})
        self.assertEqual(len(text), CP.READ_CAP)
        self.assertEqual(edges, {"a.py": {"helper.py"}})
        self.assertEqual(requests, [CP.READ_CAP, CP.HEADER_BYTES])

    def test_large_unchanged_files_do_not_allocate_file_sized_buffers(self):
        size = 8 * 1024 * 1024
        path = self.put("large.bin", b"x" * size)
        expected = hashlib.sha256(b"x" * size).hexdigest()
        tracemalloc.start()
        try:
            baseline = DC.snapshot_tree(self.root)
            entries = DC.collect_changed_entries(self.root, self.root, baseline=baseline)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(baseline["large.bin"], (expected, False))
        self.assertEqual(entries, {})
        self.assertLess(peak, 2 * 1024 * 1024)
        # Same-size, same-mtime edits in the LAST chunk still enter the proposal.
        before = path.stat()
        with path.open("r+b") as fh:
            fh.seek(-1, os.SEEK_END)
            fh.write(b"y")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        changed = DC.collect_changed_entries(self.root, self.root, baseline=baseline)
        self.assertEqual(changed["large.bin"], b"x" * (size - 1) + b"y")

    def test_large_mode_only_changes_are_preserved(self):
        data = b"x" * (300 * 1024)
        path = self.put("large.sh", data)
        baseline = DC.snapshot_tree(self.root)
        path.chmod(0o755)
        entries = DC.collect_changed_entries(self.root, self.root, baseline=baseline)
        self.assertEqual(entries, {"large.sh": data})
        self.assertEqual(entries.executable, frozenset({"large.sh"}))


class OutputRetention(unittest.TestCase):
    def test_tail_is_exact_and_every_callback_line_is_delivered(self):
        lines = [f"line {i}: ação" for i in range(2000)]
        expected = "\n".join(lines) + "\n"
        seen = []
        result = run_streaming(
            [sys.executable, "-c", "for i in range(2000): print(f'line {i}: ação')"],
            on_line=seen.append, output_limit=4000,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.output, expected[-4000:])
        self.assertEqual(seen, lines)

    def test_newline_free_output_memory_is_bounded(self):
        tracemalloc.start()
        try:
            result = run_streaming(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 8_000_000 + 'END')"],
                output_limit=4000,
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.output, "x" * 3997 + "END")
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_zero_discards_output_but_still_calls_observer(self):
        seen = []
        result = run_streaming([sys.executable, "-c", "print('hello')"],
                               output_limit=0, on_line=seen.append)
        self.assertEqual(result.output, "")
        self.assertEqual(seen, ["hello"])

    def test_default_keeps_complete_output(self):
        result = run_streaming([sys.executable, "-c", "print('x' * 100_000)"])
        self.assertEqual(result.output, "x" * 100_000 + "\n")

    def test_negative_limit_is_rejected_before_starting_process(self):
        with self.assertRaises(ValueError):
            run_streaming(["must-not-run"], output_limit=-1)


class EventProcessing(ScratchTest):
    def test_burst_keeps_order_partial_lines_and_multibyte_text(self):
        path = self.put("stream.ndjson", b"")
        log = E.RunEventLog("burst", self.root / "logs")
        seen = []
        log.subscribe(seen.append)
        tailer = E.StreamTailer(path, log)
        rows = [json.dumps({"type": "assistant", "content": [
            {"type": "text", "text": f"ação {i}"}
        ]}, ensure_ascii=False).encode() for i in range(1000)]
        payload = b"\n".join(rows)
        # Split a UTF-8 code point between pumps and leave the last row without LF.
        split = payload.index("ç".encode()) + 1
        path.write_bytes(payload[:split])
        tailer._pump()
        with path.open("ab") as fh:
            fh.write(payload[split:])
        tailer.drain()
        tailer.drain()  # no duplicate final event
        self.assertEqual([e["payload"]["text"] for e in seen], [f"ação {i}" for i in range(1000)])

    def test_oversized_line_suffix_is_dropped_until_newline_across_polls(self):
        path = self.put("stream.ndjson", b"x" * 501)
        log = E.RunEventLog("oversized", self.root / "logs")
        seen = []
        log.subscribe(seen.append)
        tailer = E.StreamTailer(path, log, max_line_bytes=500)
        tailer._pump()
        fake = json.dumps({"type": "result", "usage": {"input_tokens": 999}}).encode()
        with path.open("ab") as fh:
            fh.write(fake + b"\n")  # a suffix of the oversized row, NOT a result
            fh.write(b'{"type":"result","usage":{"input_tokens":1}}\n')
        tailer.drain()
        self.assertEqual([e["kind"] for e in seen], ["worker.raw", "worker.result"])
        self.assertEqual(seen[-1]["payload"]["input_tokens"], 1)

    def test_history_and_event_loaders_stream_and_skip_non_objects(self):
        log = E.RunEventLog("load", self.root / "logs")
        data = b'{"kind":"run"}\nnull\n[]\nnot json\n{"kind":"settle"}'
        log.path.write_bytes(data)
        self.put(H.RUNS_FILE, data)
        with patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")):
            self.assertEqual(E.load_run_events("load", self.root / "logs"),
                             [{"kind": "run"}, {"kind": "settle"}])
            with patch.object(H, "HISTORY_DIR", self.root):
                self.assertEqual(H.load_events(("run",)), [{"kind": "run"}])

    def test_truncated_hunk_still_counts_all_changes(self):
        new = "".join(f"line {i}\n" for i in range(2000))
        hunk = E.edit_hunk("", new)
        self.assertEqual(hunk["add"], 2000)
        self.assertEqual(hunk["del"], 0)
        self.assertEqual(len(hunk["hunk"]), E.HUNK_LIMIT)
        self.assertTrue(hunk["hunk"].endswith("…"))


class PlatformCopy(ScratchTest):
    def test_linux_does_not_spawn_unsupported_clonefile_probe(self):
        self.put("src/a.py")
        self.put("top.txt")
        dest = self.root / "dest"
        # Keep dest outside src so it cannot copy itself.
        source = self.root / "src"
        real = S.subprocess.run
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return real(argv, **kw)

        with patch.object(S.sys, "platform", "linux"), patch.object(S.subprocess, "run", side_effect=run):
            S.fast_copytree(source, dest)
        self.assertEqual((dest / "a.py").read_bytes(), b"x = 1\n")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-c", calls[0])


class StreamingStatus(ScratchTest):
    def test_status_memory_does_not_grow_with_gate_log_and_rereads_disk(self):
        log = E.RunEventLog("20260912-000000-aaaaaa", self.root)
        line = json.dumps({"kind": "gate.line", "ts": 11,
                           "payload": {"line": "x" * 2000}}) + "\n"
        with log.path.open("w", encoding="utf-8") as fh:
            fh.write('{"kind":"phase.start","ts":10,"payload":{"label":"gate"},"attempt":2}\n')
            for _ in range(4000):
                fh.write(line)
            fh.write('{"kind":"run.summary","ts":12,"payload":{"succeeded":true}}\n')
        tracemalloc.start()
        try:
            row = ST.runs_status(self.root)[0]
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(row["events"], 4002)
        self.assertEqual(row["elapsed_s"], 2.0)
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual((row["phase"], row["attempt"]), ("gate", 2))
        self.assertLess(peak, 2 * 1024 * 1024)
        with log.path.open("a", encoding="utf-8") as fh:
            fh.write('{"kind":"run.summary","ts":13,"payload":{"succeeded":false}}\n')
        self.assertEqual(ST.runs_status(self.root)[0]["state"], "failed")

    def test_one_pass_telemetry_matches_list_and_behavior_results(self):
        events = [
            {"kind": "phase.start", "payload": {"label": "worker"}},
            {"kind": "worker.tool", "payload": {"tool": "Read"}},
            {"kind": "worker.tool", "payload": {"tool": "Edit"}},
            {"kind": "phase.start", "payload": {"label": "review"}},
            {"kind": "worker.tool", "payload": {"tool": "Bash"}},
            {"kind": "worker.result", "payload": {"input_tokens": 10, "output_tokens": 3,
             "cache_read_input_tokens": 2, "total_cost_usd": 0.5, "model": "model-a"}},
        ]
        actual = ST.run_telemetry(iter(events))
        self.assertEqual(actual, ST.run_telemetry(events))
        self.assertEqual(actual["explore_calls"], 1)
        self.assertEqual(actual["review_tools"], 1)
        self.assertEqual(actual["tokens_in"], 10)
        self.assertEqual(actual["tokens_out"], 3)
        self.assertEqual(actual["tokens_cached"], 2)
        self.assertEqual(actual["cost_usd"], 0.5)
        self.assertEqual(actual["models"], ["model-a"])


if __name__ == "__main__":
    unittest.main()
