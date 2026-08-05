"""Scratch directories that clean themselves up when the run ends.

The suite created its throwaway trees with a bare `tempfile.mkdtemp()` and
never removed them: 88 directories left behind per run, in the developer's temp
root or the CI runner's. Nothing broke, so nothing complained — it is litter,
and litter is what hid a real defect once already (a stray tree is
indistinguishable from one a test meant to leave).

Cleanup is registered with atexit rather than per-test teardown because most of
these are created in module-level helpers with no TestCase to hang an
addCleanup on. That is the right scope anyway: test scratch is worthless the
moment the process ends, and nothing can still be holding a directory by then.

Not named test_*.py so unittest discovery leaves it alone.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_created: list[str] = []


def mkdtemp(*args, **kwargs) -> str:
    """`tempfile.mkdtemp`, remembered for removal at interpreter exit."""
    path = tempfile.mkdtemp(*args, **kwargs)
    _created.append(path)
    return path


def isolate_runs_dir() -> str:
    """Point SHEPHERD_DEV_RUNS_DIR at throwaway scratch for this process.

    A test that shells out to the CLI inherits this environment, so its run
    logs land in scratch instead of ~/.shepherd-dev/runs — the developer's
    REAL history. Without it the suite silently interleaves its own runs with
    the user's: a `run` of "add a comment to a.py" against a fixture repo sits
    in the same directory, same naming, as their actual work.

    That is the litter this module exists to stop, one level up, and it has
    already cost something: those fixture runs were read as evidence that
    shepherd's event logging was inconsistent between real runs (some logs had
    1800 events, these had 3) when the only thing they showed was that a test
    had written there.

    Call from setUpModule so a test added later cannot forget it.
    """
    os.environ["SHEPHERD_DEV_RUNS_DIR"] = mkdtemp(prefix="shepherd-runs-")
    return os.environ["SHEPHERD_DEV_RUNS_DIR"]


@atexit.register
def _cleanup() -> None:
    for path in _created:
        shutil.rmtree(path, ignore_errors=True)
    _created.clear()
