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
