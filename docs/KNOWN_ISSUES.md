# Known issues

Open defects, with what is actually established about each. An entry stays open
until a test pins the fix, then moves to the bottom with the mechanism kept —
the mechanism is the part worth remembering.

## Open

None.

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
