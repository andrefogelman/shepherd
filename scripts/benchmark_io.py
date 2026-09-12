"""Offline I/O benchmarks. Run on the approved REMOTE test machine.

Use the same script with --source-root pointing to the baseline and modified
checkouts. It imports only that checkout and its installed dependencies; it
does not launch agents, contact APIs, or use persistent Shepherd state.

python scripts/benchmark_io.py --source-root /path/to/checkout --repeat 3
"""

from __future__ import annotations

import argparse
import inspect
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path


def measure(name, action, repeat):
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        action()
        samples.append(time.perf_counter() - start)
    # Allocation tracing is separate: tracemalloc distorts runtime itself.
    tracemalloc.start()
    try:
        action()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {"name": name, "median_s": statistics.median(samples),
            "samples_s": samples, "peak_python_bytes": peak}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    source_root = args.source_root.resolve()
    if not (source_root / "src/shepherd_dev").is_dir():
        parser.error("--source-root must contain src/shepherd_dev")
    sys.path.insert(0, str(source_root / "src"))

    from shepherd_dev.contextpack import build_pack, scan_repo
    from shepherd_dev.diffcollect import collect_changed_entries, snapshot_tree
    from shepherd_dev.events import StreamTailer
    from shepherd_dev.procstream import run_streaming
    from shepherd_dev.status import runs_status

    with tempfile.TemporaryDirectory(prefix="shepherd-io-benchmark-") as tmp:
        root = Path(tmp)
        repo = root / "repo"
        repo.mkdir()
        for i in range(200):
            (repo / f"source_{i:04}.py").write_text(
                "# orders\n" + "def orders():\n    return 1\n" * 200,
                encoding="utf-8",
            )
        for i in range(40):
            noise = repo / "node_modules" / f"pkg{i}"
            noise.mkdir(parents=True)
            for j in range(100):
                (noise / f"noise{j}.py").write_bytes(b"# dependency noise\n")

        large = root / "large"
        large.mkdir()
        with (large / "large.bin").open("wb") as fh:
            for _ in range(32):
                fh.write(b"x" * (1024 * 1024))
        baseline = snapshot_tree(large)

        stream = root / "stream.ndjson"
        with stream.open("w", encoding="utf-8") as fh:
            for i in range(20_000):
                fh.write(json.dumps({"type": "result", "usage": {"input_tokens": i}}) + "\n")

        class Sink:
            # Isolate parser throughput from filesystem/event-observer cost.
            def emit(self, *args, **kwargs):
                pass

        def tail_burst():
            StreamTailer(stream, Sink()).drain()

        logs = root / "logs"
        log = logs / "20260912-000000-aaaaaa" / "events.ndjson"
        log.parent.mkdir(parents=True)
        with log.open("w", encoding="utf-8") as fh:
            fh.write('{"kind":"phase.start","ts":10,"payload":{"label":"gate"}}\n')
            line = json.dumps({"kind": "gate.line", "ts": 11,
                               "payload": {"line": "x" * 400}}) + "\n"
            for _ in range(20_000):
                fh.write(line)
            fh.write('{"kind":"run.summary","ts":12,"payload":{"succeeded":true}}\n')

        def output_tail():
            # Baseline callers retained everything and then sliced to 4000.
            kw = {"output_limit": 4000} if "output_limit" in inspect.signature(run_streaming).parameters else {}
            result = run_streaming(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 16_000_000)"], **kw
            )
            if result.returncode != 0 or result.output[-4000:] != "x" * 4000:
                raise RuntimeError("output benchmark did not produce the expected tail")

        cases = [
            ("context_scan_ignored_tree", lambda: scan_repo(repo)),
            ("context_pack", lambda: build_pack(repo, "orders")),
            ("diff_snapshot_ignored_tree", lambda: snapshot_tree(repo)),
            ("snapshot_32mib", lambda: snapshot_tree(large)),
            ("unchanged_diff_32mib", lambda: collect_changed_entries(large, large, baseline=baseline)),
            ("worker_stream_20000_events", tail_burst),
            ("status_20000_gate_lines", lambda: runs_status(logs)),
            ("subprocess_16mb_output_tail", output_tail),
        ]
        results = []
        for name, action in cases:
            result = measure(name, action, args.repeat)
            results.append(result)
            print(json.dumps(result), file=sys.stderr, flush=True)
        print(json.dumps({"python": sys.version, "platform": platform.platform(),
                          "source_root": str(source_root), "repeat": args.repeat,
                          "results": results}, indent=2))


if __name__ == "__main__":
    main()
