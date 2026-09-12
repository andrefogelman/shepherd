# Repository performance analysis — 2026-09-12

Scope: `andrefogelman/shepherd`, base commit
`9e6a842bae068c3779a950152aa21d5d64530f81`. Sources are this checkout,
its committed documentation and tests, and this repository's GitHub CI.
No GBrain or other project was consulted.

**Validated implementation:** `b8fe0356c19ea9626d60f290b6277f111a6e858d`.
The user explicitly approved committing and pushing to `main` before testing
for this task. All 11 jobs in
[CI run 34701993084](https://github.com/andrefogelman/shepherd/actions/runs/34701993084)
passed: 993 tests on each of six Linux/macOS and Python 3.11/3.13/3.14
combinations; 106 focused tests repeated ten times; syntax/name lint;
lockfile consistency; and both platform benchmark jobs. All execution took
place on GitHub's remote runners. The first type-check job's success was
invalid: a subsequent coverage assertion found it had analyzed zero files.
The checker now passes the source directory explicitly and requires all 38
source files to be analyzed. Its corrected result is being verified in CI.

## Measured results

Three runtime samples per workload, median shown, using Python 3.11. Each
platform measured base and current source sequentially on the same runner.
These are synthetic I/O workloads, not end-to-end agent or LLM timings.

| Workload | Linux base → current | Speedup | macOS base → current | Speedup |
| --- | ---: | ---: | ---: | ---: |
| Context scan with ignored tree | 47.1 → 5.1 ms | 9.19× | 51.1 → 5.6 ms | 9.13× |
| Context pack | 76.6 → 34.3 ms | 2.23× | 97.6 → 30.9 ms | 3.16× |
| Diff snapshot with ignored tree | 77.0 → 10.6 ms | 7.29× | 71.4 → 8.5 ms | 8.39× |
| Snapshot, 32 MiB file | 25.7 → 23.2 ms | 1.11× | 23.5 → 18.5 ms | 1.27× |
| Unchanged diff, 32 MiB file | 25.7 → 23.3 ms | 1.10× | 28.0 → 19.6 ms | 1.43× |
| Worker stream, 20,000 events | 497.1 → 191.7 ms | 2.59× | 457.5 → 136.6 ms | 3.35× |
| Status, 20,000 gate lines | 75.3 → 56.6 ms | 1.33× | 75.2 → 38.4 ms | 1.96× |
| Subprocess, 16 MB output tail | 28.7 → 24.9 ms | 1.15× | 59.9 → 40.7 ms | 1.47× |

Peak Python allocations (measured separately from runtime; decimal units):

| Workload | Linux base → current | macOS base → current |
| --- | ---: | ---: |
| Snapshot, 32 MiB file | 33.56 MB → 269 KB | 33.56 MB → 270 KB |
| Unchanged diff, 32 MiB file | 33.56 MB → 794 KB | 33.56 MB → 795 KB |
| Worker stream | 3.15 MB → 10.8 KB | 3.15 MB → 11.2 KB |
| Status | 32.20 MB → 26.3 KB | 32.20 MB → 26.6 KB |
| Subprocess output tail | 32.12 MB → 211 KB | 32.13 MB → 224 KB |

Every measured median improved in this run. Raw samples, interpreter and
runner details, peak allocation values and source/job identifiers are saved
in [performance-results.json](2026-09-12-performance-results.json).
The samples demonstrate these workload improvements; they are not a
confidence interval or a guarantee about a different filesystem or workload.

## Findings and changes

The following are source-level findings, not timing measurements. The
complexity and allocation bounds describe the work performed by the code.

| Path | Previous work | Change | Where it matters |
| --- | --- | --- | --- |
| Context scan | Materialize and sort every `rglob` entry, including dependency/build trees, before filtering and applying the 4,000-file cap | Sort directory entries as needed, prune excluded directories before descent, stop traversal at the cap | Large repositories with `node_modules`, `.git`, build output, or many files |
| Context text/import reads | Read an entire file, then slice to 100,000 or 4,000 bytes | Request the byte limit from the file itself | Every pack; import extraction previously reread whole source files |
| Diff traversal | Traverse excluded trees and stat their files before rejecting them | Prune excluded directory names through `os.walk`; retain regular-file and symlink exclusions | Hosted-worker snapshots and proposal collection |
| Snapshot hashing | Allocate each entire file before hashing | Stream SHA-256 with `hashlib.file_digest` | Large assets and generated files in the proposal tree |
| Baseline diff | Allocate every modified-tree file, even unchanged large files | Hash files of at least 256 KiB in chunks; allocate full content only when it differs or is newly proposed | Large mostly unchanged worktrees |
| Worker event tailing | Read the entire available backlog and repeatedly split/copy its remaining suffix | Bounded reads and a mutable partial-line buffer; one pass through available bytes | Bursty or verbose worker output |
| Gate/hosted output | Retain all merged stdout/stderr, concatenate it, then return only the last 4,000 characters | Optional rolling output limit, enabled in local gates, remote gates and hosted worker execution | Noisy suites and long-running CLI workers |
| Event/history loading | Hold raw log text, split lines and decoded events simultaneously | Decode from the file iterator; list-returning APIs retain their contract | Long event/history files |
| Status | Load all events and traverse the list for summary, phase, usage and tool metrics | Aggregate in one streaming pass; select recent IDs with a bounded heap | Repeated status requests over long verbose logs |
| Edit hunk rendering | Retain the complete generated diff before truncating to 4,000 characters | Retain the displayed prefix while continuing to count all changes | Large Write/Edit events |
| Linux tree copies | Try the APFS-specific `cp -c` command for each top-level entry before the ordinary copy | Try `-c` only on Darwin; keep existing copy fallbacks | Linux clones and gate stages with many top-level entries |

### Context ordering and scope

The context walker retains component-wise `Path` ordering, including a
directory and similarly named siblings (`a/`, `a.py`, `a-/`). It preserves
the existing hidden-directory rule, `.github` exception, text extensions,
size filtering and allowed-prefix semantics. Directory symlinks are not
followed. This is a traversal optimization, not a different relevance model.

Scoring still receives the same 100,000-byte prefix; imports still receive
the same 4,000-byte prefix, decoded using the existing replacement behavior.
The existing invocation-scoped `RepoScan` behavior is unchanged. No new
cross-invocation cache was introduced.

### Content and mode integrity

All baseline comparisons remain content-based. An unchanged `(size, mtime)`
pair does not suppress a read. Executable-only changes still enter the
proposal and retain their metadata. The snapshot still refers to the clone's
original contents rather than a concurrently edited live repository.

Large changed files can require a second read to materialize their proposal
bytes. That is a deliberate memory/IO tradeoff: unchanged files no longer
require file-sized allocations, while the public proposal API still receives
complete bytes. Benchmark both mostly unchanged trees and change-heavy
workloads before making throughput claims.

### Streaming and observability

`run_streaming(..., output_limit=None)` still returns full output. Callers
that only expose a tail explicitly request 4,000 characters. Line observers
still receive every complete line, including early failing-test messages
that later scroll out of the retained tail. Process-group timeout handling,
environment handling and gate verdict decisions retain their existing code.

Without a line observer, subprocess output is read in 64 KiB character
chunks. With an observer, a single arbitrarily long line still needs to be
materialized to preserve the callback contract. The rolling output bound
does not bound arbitrary callback allocations or subprocess memory.

The worker tailer reads at most 64 KiB per operation, retaining at most its
configured line limit plus a read chunk before rejecting oversized input.
It snapshots the available file length for each pump so continuous appends
cannot extend that pump indefinitely. Pump/drain access is serialized.

Oversized-line handling also fixes a framing defect: clearing an oversized
prefix previously allowed a valid-looking suffix arriving in a later poll to
be parsed as a separate result event. The reader now discards through the
next newline before accepting another record.

Status rereads disk on every request. It retains counters, model names, and
the latest phase/summary; it does not retain gate lines or use a result cache.
Its auxiliary memory scales with distinct models and selected runs, plus the
largest decoded event, rather than the complete log. History and trace
list-returning APIs necessarily still retain their requested decoded events.

## Execution-path review

The current code already implements the following mechanisms described in
the older repository acceleration study:

- Single-run context/planning and gate resolution overlap in `cli.py`.
- `runN` shares the initial scan and builds feature packs concurrently.
- `run2` and best-of reuse staged gate bases.
- Best-of serializes gates while overlapping candidate reviews.
- Speculative review remains opt-in; existing cleanup and result handling
  are outside this patch's changes.

The analysis also examined clone/copy call sites, hosted worker execution,
remote gate staging/teardown, and event/status consumers. It did not change
review policy, model selection, retry limits, sandboxing, settlement or gate
serialization. There are no live provider timing measurements in this study;
the older context-pack benchmark in this repository is historical evidence,
not a measurement of today's patch or a basis for an end-to-end speedup claim.

## Verification and reproducible measurement

The exact base commit has a successful
[repository CI run](https://github.com/andrefogelman/shepherd/actions/runs/33625609713).
The run's job results were checked: all six Linux/macOS and Python
3.11/3.13/3.14 combinations, the repeated timing tests, and lockfile check
succeeded. This does **not** validate the working-tree changes.

`git diff --check` passed for the implementation. No local tests, benchmarks
or package installation were performed. Commit/push is authorized for remote
CI validation; no deployment or release is part of this task.

Twenty focused regression tests were added in `tests/test_io_perf.py`:

- Pruning ignored subtrees, sorted path order, early scan cap, allowed
  prefixes, hidden directories, symlink exclusions and custom ignore sets.
- Bounded scoring/import reads, unchanged-file allocation, same-size and
  same-mtime last-chunk edits, and executable-only changes.
- Exact Unicode output tails, callback completeness, newline-free output,
  zero/full capture and invalid output limits.
- Burst event ordering, partial UTF-8, final unterminated lines, idempotent
  drain, oversized-record framing, streamed log loading and complete hunk
  counts despite truncation.
- Linux copy process count, bounded status allocations, fresh status reads
  and one-pass telemetry.

On the approved remote host, run these checks sequentially with the existing
project interpreter and its dependencies, after checking for another heavy
job on that host:

```sh
env -u PYTHONPATH python -m unittest discover -s tests -p test_io_perf.py -v
env -u PYTHONPATH python -m unittest discover -s tests -v
```

The suite includes the repository's source-level undefined-name check.
The CI static job runs Ruff's syntax/name checks and Pyright using the same
interpreter and configuration for base and current source. New type-error
diagnostics fail the job; existing diagnostics are counted explicitly, not
presented as a clean type check. File, rule, message and multiplicity enter
the comparison; line-number shifts do not.

`scripts/benchmark_io.py` provides eight offline workloads covering context
scan/pack construction, ignored-tree snapshots, 32 MiB snapshots/diffs,
20,000 worker events, status over 20,000 gate lines, and 16 MB subprocess
output. Fixture construction is outside measurement. Runtime samples are
separate from peak Python-allocation measurements to avoid conflating
tracemalloc overhead with elapsed time.

Use this same script and interpreter against two source directories: the
base revision above and the modified source. No branches are needed. Run the
two measurements sequentially on the same remote filesystem:

```sh
python /path/to/modified/scripts/benchmark_io.py --source-root /path/to/base --repeat 3
python /path/to/modified/scripts/benchmark_io.py --source-root /path/to/modified --repeat 3
```

It emits JSON containing individual samples, median runtime, peak Python
allocations, interpreter, platform and selected source root. It does not
launch an agent or access network APIs. The output-tail case adapts to the
old runner's full-capture API so both revisions exercise the same caller
contract. Peak allocations do not represent RSS, filesystem cache or child
process memory. These synthetic workloads establish overhead reductions;
they cannot establish an LLM run's end-to-end acceleration.
