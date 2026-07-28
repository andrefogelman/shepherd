# Acceleration study + bug audit (2026-07-27)

Dual audit of shepherd-dev 0.1.27: (1) where run wall-clock goes and how to cut it
without weakening any premise; (2) bug hunt across the codebase.

**Method.** Two independent read-only investigations (pipeline mapping + code
review with a full suite run), then an adversarial second review of the findings
against the code, with each disputed point re-verified line by line. Corrections
from that second pass are incorporated here — this document is the final
verdict, not the raw first report.

**Inviolable premises** (the filter for every acceleration below): human-only
settlement; every proposal gated on the repo's real test suite; worker
sandboxing/isolation (L1 clone, Landlock/Seatbelt); deterministic supervisor;
skeptical LLM review; structured retries on a clean base; worktree as source of
truth; custody-based reviewer isolation.

## Test suite status

386 passed, 52 subtests passed — **but only with a clean environment**. This
machine exports `PYTHONPATH=/home/andre/.hermes/hermes-agent`, and with it the
suite is red: `test_catestudy.py::GateDepDirsTests::{test_plain_materialize_gate_sees_deps,
test_staged_gate_sees_deps}` fail with `ModuleNotFoundError: No module named
'tests.test_dep'` inside the gate subprocess. This is bug B4 manifesting live —
the green number is conditional on `env -u PYTHONPATH`.

## Part 1 — Where the wall-clock goes

Order of magnitude, per `run` attempt:

1. **Worker LLM** — dominant: 128.6s–448.7s (context pack A/B benchmark). Already
   attacked by the pack (−71%).
2. **Review LLM** — a second full `workspace.run`, serial after the gate.
3. **Gate** — the suite itself (incompressible without violating premises) plus
   tree copies around it (seconds to tens of seconds per call, multiplied by
   attempts/repairs/candidates).
4. **Serial startup** — planning prefetch (worst case 60s, `planning.py:21`),
   duplicated repo scans, `shepherd init`, ssh preflight: typically 5–20s.

The shipped accelerators (context pack, gate warmup, `LocalGateStage`,
speculative review in `run`, budget hard-kill) cover the single-worker path.
The gaps are in the parallel modes and the hosted providers.

## Part 2 — Acceleration opportunities (premise-safe), ranked

### A1 — best-of: pipeline gates and reviews [high]

`parallel.py:759-806` runs K × (gate + review) strictly in series. The gate is
CPU-bound, the review is network-bound (LLM): run gates under the `gate_lock`
pattern runN already uses (`parallel.py:543`) and reviews in threads. The
deterministic ranking (`parallel.py:810-813`) consumes the same data. Hides up
to (K−1)× review latency (minutes each). Every candidate is still gated on the
real suite and reviewed — only temporal overlap changes.

### A2 — run2/best-of: gate warmup for the combined gate and repairs [high]

`parallel.py:312` and `parallel.py:347` call `_run_gate` without `stage`/`warmup`,
falling to a full `_materialize` per gate (`supervisor.py:841-850`). The
mechanism already exists: `LocalGateStage` (`supervisor.py:268-319`) is built in
the background while the worker runs and cuts each gate to metadata clone +
overlay. Start one alongside the workers in `develop_parallel`, and per
candidate in best-of (a shared pristine base built while the K workers run).

### A3 — speculative review in run2 and hosted/codex [medium-high]

The review∥gate overlap exists only in `run`, opt-in (`--speculative-review`,
`supervisor.py:1066-1084`, `cli.py:733`). run2 reviews strictly post-gate
(`parallel.py:354-365`); hosted/codex likewise (`hosted.py:299-304`). Extend the
same flag — keep it opt-in, since a gate failure wastes the review tokens.

### A4 — runN: per-feature packs in parallel + single repo scan [high for runN]

`cli.py:1139-1144` builds each feature's pack serially; each `_build_pack` is a
planning subprocess (60s timeout) plus two full repo scans (`cli.py:477` →
`contextpack.py:288-291`, then `contextpack.py:344`). The scan is
feature-independent. Share one scan and thread the n plannings: 3 features go
from worst case ~180s to ~60s + 1 scan. Deterministic precompute; no
supervision phase changes.

### A5 — overlap run startup phases [medium]

`cli.py:663-690`: ssh preflight, planning subprocess, `_refresh_substrate` are
independent and queued before the worker exists. Thread them; the worker still
launches only when everything is ready. Typically 5–15s per run.

### A6 — hosted path (grok/codex) inherits no accelerator [medium, provider-specific]

(a) full `shutil.copytree` per attempt on the critical path (`hosted.py:230` →
`123-133`) — not even `fast_copytree`; (b) gate without stage (`hosted.py:279`);
(c) `collect_changed_entries` byte-compares both entire trees per attempt
(`diffcollect.py:40-56`) — skip files with identical (size, mtime); (d) serial
review (A3). Isolation, gate and review unchanged — only staging gets cheaper.

### A7 — smaller items

- `_build_pack` scans the repo twice (`cli.py:477` + `contextpack.py:344`):
  pass the first scan through (~1–2s/run).
- One `LocalGateStage` per run instead of per attempt (`supervisor.py:928`,
  closed after single use at `836-837`): saves one full tree copy per retry,
  mostly hidden behind the worker; relieves concurrent I/O in runN lanes.
- Watchdog grace 60s → ~15s (`worker_watchdog.py:117`): up to ~45s saved per
  budget-busting attempt. The hard-kill premise gets stricter, not looser.

### Considered and REJECTED (would violate premises)

- **Auto-scoping the gate** to new/affected tests: the premise is "every
  proposal gated on the repo's real suite". `{NEW_TESTS}` (`supervisor.py:729-736`)
  exists only for the native gate (repo has no suite of its own). Rejected.
- **Caching gate results** by entries hash: useless (identical entries are
  already blocked by the no-progress guard, `supervisor.py:1027-1043`) and any
  cache skipping the suite on a new proposal violates the gate. Rejected.
- **Removing the settle-par re-gate** (`cli.py:1084-1105`): it is the guard
  that re-proves the proposal against the post-drift worktree before writing.
  Rejected.
- **Auto-settle on gate-pass without review**: `_auto_settle_conditions`
  (`cli.py:360-376`) requires review approved; relaxing violates human-only
  settlement + skeptical review. Rejected.
- **Reviewer writing the final patch**: breaks custody isolation — the
  deterministic guard requires changeset == `{REVIEW.json}`
  (`supervisor.py:654-672`). Rejected.
- **Sharing one clone between parallel workers**: the per-worker clone IS the
  isolation unit (`parallel.py:1-9`). Rejected.
- **Removing the runN gate_lock**: not a formal premise violation, but two
  concurrent CPU-heavy suites distort timing and cause spurious failures —
  deliberate design (`supervisor.py:1093-1095`). Kept.
- **Reusing the worker session between attempts**: clean-base discipline
  requires each attempt to be judged on its own merits with structured guidance
  (`supervisor.py:1190-1197`). Rejected.

### "Optimizations" that would reintroduce pinned bugs (do not do)

- Replacing `_remove_tree`'s child-interpreter spawn with in-process
  `shutil.rmtree` (`supervisor.py:230-258`): the subprocess IS the fix for
  "local gate stage killed the run while tearing itself down"
  (`KNOWN_ISSUES.md`) — in-process rmtree follows the `.venv` symlink into the
  open workspace and raises `UnscopedMutationError`.
- "Simplifying" the perl killtree/teepump (`supervisor.py:399-497`): each
  fallback preserves at least alarm+exec semantics in exotic environments.
- A cheaper reviewer placement: custody (retained output + `REVIEW.json`-only
  guard + discard) is the documented compensation for no syscall-read-only
  reviewer in lane v0.2 (`supervisor.py:627-633`).

## Part 3 — Bugs

Severity after adversarial re-review. All confirmed by tracing the code path
unless marked otherwise.

### B1 [MED-HIGH] — remote gate warmup race: gate may test a partial tree / double setup

`remotegate.py:285-287` joins the warmup thread with `self.timeout`, but
`_stage()` runs two sequential remote commands with `self.timeout` each
(`268-283`) — the thread can live up to 2× timeout. If the join expires with
the thread alive, `warmup.error` is still `None` → `run_remote_gate` adopts the
workdir (`323`) and skips the copy (`343`): the gate runs overlay+tests on an
incomplete copy, and/or runs its own `setup_cmd` (`355`, `did_setup` still
False) concurrently with the warmup's setup. `teardown()` (`289-295`) has the
same insufficient join: `rm -rf` while setup still writes. Trigger:
`{id}`-isolated `setup_cmd` slower than `timeout − copy_time`. Fix: signal real
completion (`threading.Event` at the end of `_stage`, or an unbounded join) and
treat "thread alive after join" as error — never adopt an incomplete warmup.

### B2 [MEDIUM] — hosted providers: user edits during the run are silently reverted on settle

`hosted.py:247` + `diffcollect.py:50-56`: `collect_changed_entries(repo_root,
clone)` diffs the clone against the LIVE repo, not a snapshot. If the user
edits `foo.py` after the clone, the clone carries the old content → `foo.py`
enters the proposal with stale bytes → gate passes (it tests the clone) →
settle writes the stale content, reverting the user's edit without warning.
Aggravating: hosted proposals carry no `regate_cmd` in the manifest
(`hosted.py:306-330`; only written in `parallel.py:609`), so the settle-time
re-gate guard does not apply to them. Fix: content snapshot (or mtime+hash) at
clone time with refuse/warn on drift at collect or settle; or ship `regate_cmd`
in hosted proposals.

### B3 [MEDIUM] — unwrapped `future.result()` in run2 AND best-of

Two call sites: `parallel.py:244` (develop_parallel) and `parallel.py:757`
(develop_best_of) — `[f.result() for f in futures]` bare. Any worker exception
propagates raw: no `ParallelReport`, no useful message. The late
`_clone_workspace` calls (handoff/repair, `266`, `322`) also raise outside any
try. `develop_many` does the opposite deliberately (`578-582`: "a lane crash
must not sink the others"). Fix: per-future capture like `develop_many`, and
wrap the late clones.

### B4 [MEDIUM] — the gate inherits all of `os.environ`

`supervisor.py:816-828` → `procstream.py:40-50` (`Popen` without `env`).
**Reproduced live on this machine**: inherited `PYTHONPATH` shadows the stage's
`tests` package and fails 2 suite tests (see "Test suite status"). The import-
shadowing consequence is configuration-dependent in Python (`python -m pytest`
prepends cwd), so the general dangerous leak is **stateful env** —
`DATABASE_URL`, `MIX_ENV`, `NODE_ENV`, `LD_PRELOAD` — into suites that read
them: false PASS/FALSE FAIL burning attempts, or tests touching the wrong
database. Fix: sanitize at least `PYTHONPATH`/`PYTHONHOME` and document the
gate's env contract.

### B5 [MEDIUM] — speculative review thread never joined on gate-failure paths

`supervisor.py:1081-1084` starts `spec_thread` (daemon) overlapping the gate,
but `join()` exists only at `:1139` (post-gate pass). Both failure paths exit
first: `return report` on infra error (`:1117`) and `continue` on gate failure
(`:1124`) — the latter also runs `output.discard()` (`:1120`) while the
reviewer may still be reading the changeset, and the next attempt calls
`workspace.run` while the review thread is alive inside the same workspace.
Damage depends on shepherd-ai 0.3.0 thread-safety (unknown). Fix: join (or
cancel-and-join with a short timeout) before both exits.

### B6 [MEDIUM] — adoption cache ignores edits inside untracked directories

`cli.py:160-168` (`_adoption_key`): `git status --porcelain -z` reports an
untracked dir as `?? dir/`; the stat is the directory's. Editing an existing
file inside it does not change the dir mtime → key unchanged →
`_refresh_substrate` skips re-adoption (`:207`) → the worker builds on a stale
base, silently. Fix: `git status -uall`, or treat `?? dir/` as always
re-adopt.

### B7 [LOW-MEDIUM] — `settle_proposal` without partial-write recovery

`cli.py:1107-1108`: `materialize_into` has no try/except — a mid-write failure
leaves the repo partially written and the exception raw (staging survives, so
content is not lost). `settle_run` has the full recovery pattern
(`:1361-1378`). Fix: mirror it.

### B8 [LOW] — optimize replay timeout ignores the gate

`optimize.py:197-204`: subprocess timeout = `worker_budget + 300`, but the real
worst case is `init(120s) + worker_budget + gate_timeout(600s default)` =
1200s < 1620s with defaults → replay killed mid-gate → counted as failure → a
good candidate is rejected or the guard set falsely "regressed". Fix:
`timeout = worker_budget + gate_timeout + 480`.

### B9 [LOW] — `auto_commit_branch`: on success the files vanish from the worktree

`cli.py:344-357`: commit on `shepherd/<slug>` branch → `finally` checks out the
original branch → new files disappear. The `finally` comment ("the accepted
files remain in the worktree either way") is false on the success path, and the
settle message ("N file(s) written") no longer describes the worktree. Branch
isolation looks intentional, so this is an expectation/messaging bug. Fix:
correct the comment and messages.

### B10 [LOW] — remote preflight rejects test_cmd with env prefix

`remotegate.py:135-143`: `shlex.split(test_cmd)[0]` on `MIX_ENV=test mix test`
yields `MIX_ENV=test` → `command -v MIX_ENV=test` fails → valid config
rejected. Fix: skip leading `VAR=value` tokens.

### B11 [LOW] — remote exit 124 always read as timeout

`remotegate.py:377-379`: a user test_cmd exiting 124 on its own becomes
`infra_error` → aborts the whole run instead of a retryable test failure. Fix:
distinguish via locally measured duration.

### B12 [TRIVIAL] — `--worker-budget 0` silently disables the teepump budget

`supervisor.py:473-487`: `alarm $b` is armed before the `$SIG{ALRM}` handler is
installed (post-fork). Two effects, neither the one originally reported:
(a) `alarm 0` in Perl CANCELS the timer — budget 0 means the teepump never
enforces the budget (killtree + watchdog remain as backstops); (b) the
alarm-before-handler race is real but its window is pipe+fork (sub-second), so
it only bites at `$b ≈ 1`. Fix: validate `--worker-budget >= 1` in argparse.

### Suspicions (plausible, not verified)

- Cross-process race run × settle: `_refresh_substrate` does
  `shutil.rmtree(.vcscore)` (`cli.py:211`) with no inter-process lock; a
  concurrent `settle` (or second `run`) could die mid-way or corrupt state.
- `staged._torn = True` set outside `_td_lock` (`remotegate.py:334`): latent —
  callers are single-threaded today.
- Watchdog: PID reuse between the `ps` snapshot and `os.kill`
  (`worker_watchdog.py:132-136`); children born after the snapshot escape.
- `CliGrokExecutor`/`CliCodexExecutor`: `subprocess.run(timeout=…)` kills only
  the direct child (`grok_exec.py:98-107`, `codex_exec.py:110-118`) —
  grandchildren may orphan on timeout.
- `_adoption_key` mtime-based: mtime-preserving tools (`cp -p`, some editors)
  can produce a stale key even for tracked dirty files.

## Priority

Bugs: **B2** (silent user-work revert) > **B1** (remote warmup race) >
**B3 + B5** (same shape, three sites) > **B4** > **B6** > B7 > rest.

Acceleration: **A1** (best-of pipeline), **A2** (run2/best-of warmup),
**A3** (speculative review extension), **A4** (runN parallel packs) — all
mechanically small, reusing patterns already pinned by tests in this repo.

## Corrections incorporated from the second review

For the record, the first-pass report overstated three items:

1. ~~B12 claimed budget 0 "kills the worker instantly"~~ — `alarm 0` cancels;
   the real effect is the opposite (budget not enforced). Trivial severity.
2. ~~A-reflink claimed "physical copy on every hot path in Linux" as a
   multiplier~~ — the `cp -c` fallback is intentional and documented
   (`supervisor.py:182-186`), and coreutils ≥ 9.0 `cp -R` already uses
   `copy_file_range` (reflink on btrfs/xfs; verified: this machine runs
   coreutils 9.4). Real cost is one failed exec per top-level entry. Removed
   from the ranked list.
3. B4's consequence "gate judges the installed package instead of the
   proposal" is configuration-dependent in Python; the general risk is
   stateful env leakage. Description corrected above.

It also under-reported two items, now fixed: B3 has two call sites (run2 AND
best-of), and B5 was listed as a suspicion when the code path confirms it.
