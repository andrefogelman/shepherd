# Known issues

Open defects, with what is actually established about each. An entry stays open
until a test pins the fix, then moves to the bottom with the mechanism kept —
the mechanism is the part worth remembering.

## Open

### The adoption fingerprint still trusts mtime for dirty files

`_adoption_key` hashes HEAD plus `(mtime, size)` of every dirty path. That is
the same evidence the diff fast-path was removed for: on a coarse-timestamp
filesystem a same-size rewrite can land on the recorded mtime and read as
unchanged, so a stale adoption would be reused.

Materially safer than the diff case and left open deliberately. There, the
snapshot and the worker's write were milliseconds apart by construction; here
the two key computations are whole runs apart, so a collision needs two edits
in the same jiffy in different runs. Closing it means hashing the content of
the dirty files — usually a handful, so the cost is small if it ever bites.

### The watchdog can signal a reused pid

`_read_proctable` takes a snapshot; `_kill_subtree` signals afterwards, with
three seconds between SIGTERM and SIGKILL. A pid freed in that window and
reissued would be signalled instead of the intended process.

Bounded rather than fixed: only descendants of shepherd's own pid are ever
selected, so the blast radius is this process's own tree. A real fix needs
`pidfd_open` (Linux-only), which costs the portability the watchdog is written
for. Recorded because the reasoning, not the risk, is what would otherwise be
re-litigated.

## Fixed

### The jailed worker and reviewer could not run a single command

**Was:** on macOS, every `Bash` call inside a run failed and neither agent said
so as a failure — they degraded. The worker wrote code it never executed; the
reviewer reported "Bash is blocked in this sandbox (EPERM), so this review is
by inspection of the diff against the spec — no execution." A supervised cycle
that never runs anything is most of the supervision gone, and it looked like a
clean pass.

```
EPERM: operation not permitted, mkdir '/private/tmp/claude-<uid>/<flattened-cwd>'
```

Three facts meet:

1. The worker runs under a syscall jail (`launch_confined`) whose writable
   roots are the run's clone. Anything written outside is denied, and a
   seatbelt denial surfaces to the CLI as `EPERM`.
2. The framework redirects `HOME`, `CLAUDE_CONFIG_DIR` and `TMPDIR` into a
   per-run scratch inside that clone.
3. The Claude CLI does not read `TMPDIR` for its own Bash-sandbox scratch. On
   darwin that root is a hardcoded `/tmp`; `CLAUDE_CODE_TMPDIR` is the only
   override. So the sandbox scratch was the one thing still resolving outside
   the writable roots.

Established by probe: a bare CLI in the same clone path, unjailed, runs `Bash`
fine, and the flattened scratch dir is created without complaint — so it is
neither the path shape nor a filesystem permission. Redirecting `TMPDIR` alone
does not move the sandbox scratch.

**Fix:** `_with_sandbox_tmpdir` adds `CLAUDE_CODE_TMPDIR` to the launch's env
prefix, set to whatever the framework chose for `TMPDIR` — read off the argv,
so a framework that moves its scratch moves this with it. Pinned by
`tests/test_worker_sandbox_tmpdir.py`, whose last case fails loudly if the
framework ever stops carrying `TMPDIR` there, since the rewrite would silently
degrade to a no-op and restore the defect.

### The local gate stage killed the run while tearing itself down

**Was:** any supervised cycle in an affected repo died mid-gate. The process
raised inside teardown, so the run neither passed nor failed, it just ended.

`_link_dep_dirs` symlinks the repo's dependency directories (`.venv`,
`node_modules`) into the staged copy so the gate can import them without a
reinstall. Teardown then removed the stage with
`shutil.rmtree(root, ignore_errors=True)`.

`rmtree` deletes entries by bare name against a directory file descriptor
(`os.unlink(entry.name, dir_fd=topfd)`). The substrate patches `os.unlink` and
resolves the candidate through `Path.resolve()`, which **follows the symlink**
onto the real `.venv` inside the open workspace. That is a tracked path, so the
patch demanded a scope:

```
UnscopedMutationError: Workspace mutation 'os.unlink' for .venv requires an
active scope. Wrap edits in fork()/merge() before mutating the workspace.
```

`UnscopedMutationError` derives from the substrate's error base, **not** from
`OSError`, so `ignore_errors=True` did not swallow it and the exception left
teardown and left the run.

**Blast radius, established by probe** (workspace open, cwd at the repo root,
four stage shapes torn down):

| stage holds | in-process rmtree |
| --- | --- |
| real files only | clean |
| a file whose bare name also exists at the repo root | clean |
| a symlink resolving **into** the workspace | `UnscopedMutationError` |
| a symlink resolving outside the workspace | clean |

So the trigger is the symlink, not the file name and not the working directory.
An earlier reading of this defect blamed a bare name resolved against the cwd;
that is wrong — `_resolve_path_with_dir_fd` honours `dir_fd`, and the
`name_collision` row above refutes it. The affected repos are the ones where
`_link_dep_dirs` has something to link: a `.venv` or `node_modules` at the root,
which is most Python and most Node repos. Both teardown sites were affected —
the warmed `LocalGateStage` and the plain gate's temporary directory, whose
`TemporaryDirectory` cleanup is the same in-process `rmtree`.

**Fix:** `_remove_tree` deletes a staged tree from a child interpreter, which
carries none of the patches. Pinned by `tests/test_gate_teardown.py`, including
one case against the real substrate in a throwaway workspace; reverting the fix
turns those tests red with the error above.

Verified on darwin and on Linux. The simulated guard resolves a `dir_fd` the
way the substrate does — `F_GETPATH` on darwin, the `/proc` fd link on Linux —
and one test pins that resolution by itself, so a platform where the probe
cannot resolve one fails by name instead of quietly turning the rest of the
file into tests that raise nothing and prove nothing.

### A reworded finding closed as `fixed` without being fixed

**Was:** the ledger keys a finding on the sha256 of its normalized text, and
normalization strips a leading severity label and collapses whitespace —
nothing else. So two reviews describing one problem in different words were two
different findings, and `record_round` closed the earlier one as `fixed` for the
only reason it knew: the reviewer had not raised *that string* again.

The reviewer is a language model; it rewords by default. And nothing told round
2's reviewer what round 1 had objected to, so it re-derived the objection in
fresh words — the rewording was forced by the design, not model caprice.
Observed in a run whose round 2 re-raised round 1's blocking objection verbatim
in substance:

```
[fixed] CONVENTIONS.md "Each module ships its own test file": new module
        duration.py is not accompanied by a test file …   (round 1)
[open]  BLOCKING — CONVENTIONS.md 'Each module ships its own test file': new
        module duration.py is not covered by tests.       (round 2)
```

**Blast radius, established:** the run's *decision* was unaffected. Approval
comes from the reviewer's verdict, and the rework gate is `has_open()`, which
stayed true because the reworded finding was itself open. What was wrong was the
tally's account of history — `[fixed]` on something nobody fixed — and the
`rounds` trail, which exists precisely so a human can see whether an item was
addressed once or kept coming back. A chronic finding read as one closed item
plus one new one. That is the failure the module's own docstring says it exists
to prevent.

**Fix: closure on evidence, never on silence.** Matching by meaning was rejected
outright — a similarity threshold loose enough to catch rewording will
eventually fold two genuinely different findings into one and close a real
problem in silence, which is strictly worse than a tally that over-reports
closure. So identity is not guessed. Each round is handed the still-open
findings with their ids (`Ledger.guidance()` into the review prompt's
`findings`), and the verdict answers about them: an item still present is
re-raised with its id in leading brackets, an item checked and found gone goes
in the verdict's new `resolved` list. `record_round` closes a finding only on an
explicit `resolved`, or on approval — the one judgement that covers the whole
change. A rejecting reviewer that merely stopped mentioning something closes
nothing. An unrecognised id falls back to text identity, so a hallucinated
reference cannot mint a finding keyed on the hallucination, and a finding named
in both `issues` and `resolved` stays open, since only one of the two readings
can hide a real problem.

Pinned by `tests/test_ledger.py` (`ClosureNeedsEvidenceTests`, which holds the
observed pair above verbatim) and `tests/test_review_rounds.py`
(`TheReviewerSeesTheOpenFindings` — that the prompt teaches the id protocol,
that the task accepts `findings`, that the open findings actually reach the
reviewer, and that `resolved` is read off the verdict).

### The attempt counter read as a per-round budget and was not one

**Was:** a rework round resets `attempts_left` to `max_attempts` — a round
carries its own allowance, by design — but `number`, the counter in the
`attempt N/M` label, is monotonic for the whole run and never resets. So the
second round's first attempt printed `attempt 2/2` while in fact holding a full
fresh allowance, and a run reaching a third round would have printed
`attempt 3/2`. Display only: the budget arithmetic was correct, and no attempt
was lost or gained. It was the label that described the wrong quantity.

**Fix:** `_attempt_label` numbers the attempt by its position within the round
it is actually spending — `max_attempts - attempts_left` — and names the round
whenever there has been more than one. A single-round run prints exactly what it
printed before. The gate and review lines, which used to print a bare
`attempt {number}` with no denominator, now carry the same label as the worker
line: adjacent lines describing one attempt with different numbers is how the
counter became unreadable in the first place.

Pinned by `tests/test_review_rounds.py` (`TheAttemptLabelNamesItsOwnBudget`).
One of those tests drives `develop()` through a real rework round with a
capturing reporter and asserts the old string `attempt 2/2 · worker running` is
absent — pinning the pure helper alone would have let the call sites be reverted
to the run-wide counter without a test noticing.

### `shepherd-dev optimize` crashed on every run that reached a proposal

**Was:** `KeyError: 'guidance_review'`, uncaught, from
`{k: DEFAULT_PROMPTS[k] for k in EDITABLE_KEYS}` in `optimize._propose`. The
command was dead for anyone who invoked it.

The prompts live in two copies. shepherd-ai's single-file task capture rejects
any same-package import in a task source, so `tasks.py` cannot import
`prompts.py` and carries an inlined duplicate, kept in sync by a docstring
asking politely. `guidance_review` was added to `prompts.py` and to
`optimize.EDITABLE_KEYS`, and not to `tasks.DEFAULT_PROMPTS` / `PROMPT_KEYS`.
`optimize` imports the `tasks` copy. Beyond the crash, `save_overrides` filters
by `tasks.PROMPT_KEYS` and drops anything else without a word, so even a fixed
`_propose` would have thrown a winning candidate away while reporting success.

**Fix:** the missing key added to both `tasks.PROMPT_KEYS` and
`tasks.DEFAULT_PROMPTS`, with the prompt text copied byte-for-byte from
`prompts.py`. Pinned by `tests/test_prompt_copies.py`, which is the mechanism
the polite docstring was standing in for: the two copies must agree key for key
and byte for byte, and every key `optimize` offers to edit must be both readable
from `DEFAULT_PROMPTS` and survivable through `save_overrides`.

### A hosted proposal reverted the human's edits

**Was:** editing a file while a `--provider grok` / `--provider codex` run was
in flight, then settling, silently restored the file to what it had been before
the edit. No conflict, no warning: the worker had never touched that file.

`collect_changed_entries(repo_root, clone)` compared the worker's clone to the
repo AS IT IS WHEN THE WORKER FINISHES. So the base moved under the comparison:
a file the human edited mid-run now differs from the clone's untouched copy,
which reads as "the worker changed this" and pulls it into the proposal
carrying the PRE-RUN content. Settling then writes that over the edit.

Hosted proposals also carried no `regate_cmd`, so `settle-par` skipped the
settle-time re-gate — the one check that catches a base that moved was missing
from exactly the path that races the human.

**Fix:** `snapshot_tree(clone)` pins the base before the worker starts, and the
diff is taken against that snapshot, so only files the worker actually wrote
can enter the changeset. `regate_cmd` is emitted. Pinned by
`tests/test_grok_host.py`, whose `FakeGrokExecutor.on_run` hook edits the real
repo mid-run; without the fix the edited file appears in the changeset with its
stale bytes.

### The remote gate adopted a warmup that was still staging

**Was:** with a stateful remote `setup_cmd` (a DB, containers), a run could
bring the same per-run state up twice at once, and overlay a proposal onto a
half-copied tree.

`GateWarmup.join()` waited `self.timeout`, but `_stage` makes up to TWO remote
calls bounded by that same timeout — real worst case is 2x. When the join gave
up early the thread was still copying, and `error` was still `None` because
nothing had failed YET. The caller read `error is None` as "staged and
healthy". `did_setup` was likewise still `False`, so `run_remote_gate` ran
`setup_cmd` alongside the warmup's own still-running one.

The lesson generalises past this call site: **the absence of an error is not
evidence of completion.** Anything that reports progress by mutating fields
from a thread needs a separate "finished" signal.

**Fix:** `_stage` sets a `_done` Event in a `finally`, `join()` waits against a
deadline covering the true staging budget and RETURNS whether staging finished,
and `_adoptable()` is the single predicate the gate consults. `teardown()` no
longer joins inline — removing a workdir ssh is still writing into is the other
half of the same race. Pinned by `tests/test_remotegate_warmup.py`
(`WarmupStillStagingIsNotAdopted`).

### The gate judged the proposal under shepherd's own environment

**Was:** the suite passed or failed depending on who ran it. Same commit, same
machine: green with a clean shell, two failures with a `PYTHONPATH` pointing
elsewhere.

`run_streaming` spawned the test command with no `env=`, so the gate inherited
`os.environ` verbatim. The gate exists to judge the PROPOSAL's materialized
tree, but `PYTHONPATH` puts shepherd's own source on the suite's `sys.path`,
`VIRTUAL_ENV`/`PYTHONHOME` redirect the interpreter at our checkout, and
`PYTEST_CURRENT_TEST`/`PYTEST_ADDOPTS` leak the state of whatever pytest run
launched us into the child's.

**Fix:** `gate_env()` removes that interpreter and harness state and nothing
else. The strip list is deliberately narrow — a blanket scrub would take
`PATH`, `HOME` and the project's own configuration with it, which is what every
real gate needs. `SHEPHERD_DEV_GATE_ENV_KEEP` re-admits an entry;
`SHEPHERD_DEV_GATE_ENV_STRIP` drops more. Pinned by
`tests/test_gatestream.py` (`GateEnvIsolation`), which asserts both halves: the
interpreter state is gone AND an ordinary variable still arrives.

### A diff optimization made the worker's edit disappear

**Was:** `tests/test_diffcollect.py` failed intermittently on Linux and never on
macOS. Underneath the flake: a worker's change could be dropped from the
changeset in silence.

A `(size, mtime)` fast-path dismissed a file as untouched without reading it.
But Linux stamps mtime from a coarse clock — one jiffy, 1-4ms — so a same-size
rewrite landing in the tick the snapshot was taken in carries a byte-identical
`(size, mtime)` pair. APFS timestamps are fine-grained enough that it never
reproduced on the machine it was written on.

Two things are worth keeping from this. First, it is the same
disappearing-work failure the hosted-diff entry above exists to prevent,
reintroduced as a performance tweak. Second, **a racy-window margin cannot
rescue it**: a freshly made clone's files are milliseconds old at snapshot time
— measured at 4.6ms — so every one of them falls inside any sound margin and
gets hashed anyway. The optimization is worth nothing once it is correct.

**Fix:** removed; comparison is by content hash only. Pinned by
`test_a_rewrite_of_the_same_size_on_the_same_mtime_is_still_detected`, which
forces the collision with `os.utime` rather than racing the clock, so it is
deterministic on every filesystem.

### The suite had never run anywhere but a laptop

**Was:** the defect above reached `main` because nothing else could have caught
it. The only workflow was `release.yml`, gated on `v*` tags, and the single tag
in the repo predates it — so no CI had ever run. 443 tests existed and were
executed only by whoever last touched the code, on whatever filesystem they
happened to have.

**Fix:** `.github/workflows/ci.yml` on every push and pull request, across
ubuntu (coarse mtime, GNU coreutils) and macos (nanosecond APFS, BSD userland),
on the 3.11 floor and a current interpreter. The matrix is the point, not
decoration. A second job re-runs the timing-sensitive modules ten times, which
is the cheap version of the loop that actually found the flake.

### Hosted workers orphaned their CLI's subprocesses

**Was:** a `grok` or `codex` worker that overran its budget left node processes
and MCP servers running — holding CPU, memory and API sessions with nothing
left to reap them.

`subprocess.run(timeout=...)` kills only the DIRECT child. The claude path had
solved this twice over — the launch perl puts the runner in its own session and
kills the process GROUP at the budget (#A), with the watchdog as a second layer
(#B) — and the hosted path, extracted later, inherited neither.

**Fix:** `run_cli_worker` routes both executors through
`procstream.run_streaming`, which already does `start_new_session` + `killpg`.
It lives in `hosted.py`, the provider-agnostic layer, so a provider added later
gets the guarantee instead of repeating the bug. Pinned by
`tests/test_hosted_killtree.py`, whose stand-in CLI spawns a detached
grandchild and reports its pid.

### Two commands on one repo could delete each other's workspace store

**Was:** unreproducible failures when a `run` and a `settle` overlapped, or two
runs started together.

`_refresh_substrate` rmtree's `.vcscore` and re-inits it, taking no lock. Any
concurrent reader — a settle listing runs, another run deciding whether it may
reuse the adoption — could find the directory disappearing under it, and two
runs could rmtree each other's fresh adoption.

**Fix:** `substrate_lock`, an advisory flock held across the whole sequence, so
reading the pending state, deciding on the key, the rmtree and the re-init are
one transaction. `settle_run` takes it too: locking only the writer leaves the
race intact, since the reader consumes from exactly the store the writer wipes.
The lock file lives BESIDE `.vcscore` — the file that survives the rmtree is the
only one that can guard it — and is gitignored by `init`. Best effort by
design: no `fcntl`, an unwritable repo, or a lock held past the timeout all
proceed anyway, degrading to the old behaviour rather than to a hang. Pinned by
`tests/test_substrate_lock.py`.

### `alarm` was armed before its handler, and a zero budget disabled it

**Was:** two defects in the launch perl, both around the same line.

The group-kill handler was installed only after `fork`/`pipe`, leaving a window
in which the timer was armed and `SIGALRM` still carried its DEFAULT
disposition: the process dies bare, with no group kill and no exit 124.

Separately, `--worker-budget 0` did not, as it reads, kill the worker at once.
Perl's `alarm 0` CANCELS the timer. The budget was silently NOT enforced, and
since the watchdog keys off the same number, nothing was left supervising the
run's wall clock.

**Fix:** arming before `pipe`/`fork` is load-bearing — the timer survives
`exec`, so the degenerate fallbacks still die at the budget — so the handler
moved ahead of the arming rather than the arming moving back. `$pid` is
declared up front and the closure checks definedness. A non-positive budget is
refused at the CLI and in `set_worker_budget`. Pinned by
`tests/test_teepump.py`, which asserts BOTH orderings: handler before arm, arm
before pipe/fork.

### Smaller defects fixed in the same pass

Each of these had a short mechanism and a test; recorded together because none
of them repays a section of its own.

- **A crashing worker escaped as a raw exception.** `run2` and `best-of`
  collected futures with a bare `[f.result() ...]`, which re-raises out of the
  call: no report, and the results of the workers that DID finish discarded
  with it. `develop_many` had contained this since it was written. Later
  clones — the conflict handoff and each repair round — had the same hole and
  were closed with it.
- **The speculative reviewer was never joined on the gate-failure paths.** The
  join sat only on the review path, which both gate failures jump over, so a
  daemon thread stayed inside `run_review` holding the workspace — racing
  `output.discard()` and overlapping the NEXT attempt's `workspace.run`.
- **`settle-par` had no recovery for a partial write.** An `OSError` partway
  through `materialize_into` propagated out with the worktree half-written and
  nothing said. It now keeps the stage (so nothing is lost, the content being
  still on disk) and reports which files landed.
- **Auto-commit deleted the accepted files from the worktree.** Committing them
  onto `shepherd/<slug>` and checking the original branch back out removes
  them, since that is where they now exclusively live — contradicting the
  message telling the user to review them.
- **The adoption fingerprint missed edits inside untracked directories.**
  `git status --porcelain` collapses one to `?? dir/`, and a DIRECTORY's mtime
  does not move when a file inside is rewritten in place. `-uall` lists the
  files themselves.
- **The CRO replay was killed partway through its gate.** The parent timeout
  was `worker_budget + 300`, which does not cover even the gate's default 600s,
  and the bare `except` folded the `TimeoutExpired` into a candidate failure it
  never had.
- **Remote preflight rejected `MIX_ENV=test mix test`.** `shlex.split(...)[0]`
  is the assignment, not the binary, so preflight demanded a command named
  `MIX_ENV=test`.
- **Every remote exit 124 was read as a timeout.** `timeout` propagates the
  command's own status verbatim, so a suite exiting 124 became an
  `infra_error` that aborts the run instead of a retryable gate failure.
  Elapsed time separates the two.
- **`run2` stopped emitting `review.verdict`.** A restructuring left the
  emission under `else:` (no review task), where the verdict is always `None` —
  dead code, and the verbose log silently lost the reviewer's outcome.
