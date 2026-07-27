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
import shutil
import tempfile

_created: list[str] = []


def mkdtemp(*args, **kwargs) -> str:
    """`tempfile.mkdtemp`, remembered for removal at interpreter exit."""
    path = tempfile.mkdtemp(*args, **kwargs)
    _created.append(path)
    return path


@atexit.register
def _cleanup() -> None:
    for path in _created:
        shutil.rmtree(path, ignore_errors=True)
    _created.clear()
