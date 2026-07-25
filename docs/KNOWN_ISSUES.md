# Known issues

Open defects, with what is actually established about each. An entry stays here
until a test pins the fix.

## The local gate stage can kill the run while tearing itself down

**Status:** open. **Severity:** blocks any supervised cycle in an affected repo —
the process dies mid-gate, so the run neither passes nor fails, it just ends.

`_link_dep_dirs` symlinks the repo's dependency directories (`.venv`,
`node_modules`) into the staged copy so the gate can import them without a
reinstall. `LocalGateStage.close()` then tears the stage down with
`shutil.rmtree(root, ignore_errors=True)`.

`rmtree` walks with directory file descriptors and unlinks by **bare name**
(`os.unlink(entry.name, dir_fd=topfd)`). The substrate monkeypatches `os.unlink`
in the parent process; the patched version does not honour `dir_fd`, so it
resolves the bare name against the current working directory — the real repo —
and raises on what looks like an unscoped mutation of a tracked path:

```
UnscopedMutationError: Workspace mutation 'os.unlink' for .venv requires an
active scope. Wrap edits in fork()/merge() before mutating the workspace.
```

`UnscopedMutationError` derives from the substrate's error base, **not** from
`OSError`, so `ignore_errors=True` does not swallow it and the exception
propagates out of teardown and out of the run.

Four links, all verified: the symlink is created, `rmtree` unlinks by bare name,
the patched `os.unlink` resolves against the cwd, and the exception type escapes
`ignore_errors`.

**What is not established:** the blast radius. Earlier runs against a different
repository completed their gates, so something differentiates the two cases and
that difference has not been investigated. Do not read this entry as "affects
every repo" or as "affects only repos with a `.venv`" — neither has been shown.

Worth stating plainly: a supervised development layer that cannot complete a gate
on its own source tree is not dogfooding itself. Every assertion about the
`--review-rounds` loop today comes from tests that drive `develop()` with a
stubbed substrate. The rework round has never fired against a real worker.
