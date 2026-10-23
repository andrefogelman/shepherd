"""Start a test with nothing resolved and no environment kept, and put back what was there.

`programs.resolve()` and shepherd-ai's `_jail_exposure.resolve_programs()` keep
the environment and the programs they found for the rest of the process: one
command per process in use. The suite runs hundreds of commands in one process,
so the first kept environment answered every later host-side read, and a test
that set its own `SHEPHERD_DEV_RUNS_DIR` read the developer's real run records
instead (`test_verbose`, measured).

Not named test_*.py so unittest discovery leaves it alone.
"""

from __future__ import annotations

import unittest


def fresh_programs(test: unittest.TestCase):
    """Clear both caches for `test` and restore them in its cleanup. Returns
    (memlimit, programs, _jail_exposure) for tests that inspect them; the
    shepherd-ai part is skipped when the substrate is absent."""
    from shepherd_dev import memlimit, programs

    try:
        from vcs_core import _jail_exposure
    except Exception:  # pragma: no cover - substrate absent
        _jail_exposure = None

    saved = (dict(programs._found), programs._environ)
    fork = (
        (dict(_jail_exposure._PROGRAMS), getattr(_jail_exposure, "_ENVIRON", None))
        if _jail_exposure is not None
        else None
    )

    def restore():
        programs._found.clear()
        programs._found.update(saved[0])
        programs._environ = saved[1]
        if fork is not None:
            _jail_exposure._PROGRAMS.clear()
            _jail_exposure._PROGRAMS.update(fork[0])
            _jail_exposure._ENVIRON = fork[1]

    test.addCleanup(restore)
    programs._found.clear()
    programs._environ = None
    if _jail_exposure is not None:
        _jail_exposure._PROGRAMS.clear()
        _jail_exposure._ENVIRON = None
    return memlimit, programs, _jail_exposure
