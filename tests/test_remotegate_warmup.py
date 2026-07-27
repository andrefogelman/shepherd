"""Tests for #2: speculative remote-gate warmup.

Reuses the faithful fake-ssh (join args after host, run via sh -c on the real FS)
so the background copy/setup actually happen against temp dirs. Covers: staging
copy + per-{id} setup for isolated configs, copy-only for non-isolated, gate
consuming a warmup, fallback on a failed warmup, and orphan-free teardown of an
unconsumed warmup. Runnable with: python -m unittest tests.test_remotegate_warmup
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev import remotegate as RG  # noqa: E402
from shepherd_dev.remotegate import (  # noqa: E402
    GateWarmup,
    _adoptable,
    parse_remote_config,
    run_remote_gate,
)

_real_run = subprocess.run


def _fake_ssh_base(_cfg):
    return ["__FAKESSH__"]


def _patched_run(argv, **kw):
    if isinstance(argv, list) and argv and argv[0] == "__FAKESSH__":
        return _real_run(["sh", "-c", " ".join(argv[1:])], **kw)
    return _real_run(argv, **kw)


_real_popen = subprocess.Popen


def _patched_popen(argv, **kw):
    # The streamed remote test step (procstream) launches via Popen; give it
    # the same real-ssh join + re-tokenization semantics as _patched_run.
    if isinstance(argv, list) and argv and argv[0] == "__FAKESSH__":
        return _real_popen(["sh", "-c", " ".join(argv[1:])], **kw)
    return _real_popen(argv, **kw)


def _warm_checkout() -> Path:
    warm = Path(mkdtemp())
    (warm / "src").mkdir()
    (warm / "src" / "a.py").write_text("V = 1\n")
    return warm


class RemoteGateWarmup(unittest.TestCase):
    def setUp(self):
        from shepherd_dev import procstream as PS

        RG._ssh_base = _fake_ssh_base
        RG.subprocess.run = _patched_run
        PS.subprocess.Popen = _patched_popen
        self.db = Path(mkdtemp())          # stands in for a DB/service store
        self.warm = _warm_checkout()
        self.wbase = Path(mkdtemp())

    def tearDown(self):
        from shepherd_dev import procstream as PS

        RG.subprocess.run = _real_run
        PS.subprocess.Popen = _real_popen

    def _cfg(self, *, isolated: bool):
        # isolated setup keys state on {id}; non-isolated writes a shared marker
        suffix = "d_{id}" if isolated else "shared"
        cfg = parse_remote_config({
            "ssh": "root@host", "repo_dir": str(self.warm),
            "copy_cmd": "cp -R {repo} {workdir}",
            "setup_cmd": f"mkdir -p {self.db}/{suffix} && echo up > {self.db}/{suffix}/s",
            "test_cmd": f"test -f {self.db}/{suffix}/s && grep -q 'V = 42' src/a.py && echo GATE_OK",
            "teardown_cmd": f"rm -rf {self.db}/{suffix}",
            "workdir_base": str(self.wbase),
        }, "python")
        assert cfg is not None
        return cfg

    def test_isolated_warmup_stages_copy_and_setup(self):
        cfg = self._cfg(isolated=True)
        w = GateWarmup(cfg, timeout=30).start()
        w.join()
        self.assertIsNone(w.error, w.error)
        self.assertTrue(w.did_setup)                       # {id}-isolated => setup ran
        self.assertTrue((Path(w.workdir) / "src" / "a.py").exists())  # copy done
        self.assertTrue((self.db / f"d_{w.run_id}" / "s").exists())   # per-{id} state
        w.teardown()
        self.assertFalse((self.db / f"d_{w.run_id}").exists())        # setup torn down
        self.assertFalse(Path(w.workdir).exists())                    # workdir removed

    def test_non_isolated_warmup_copies_but_skips_setup(self):
        cfg = self._cfg(isolated=False)
        w = GateWarmup(cfg, timeout=30).start()
        w.join()
        self.assertIsNone(w.error, w.error)
        self.assertFalse(w.did_setup)                      # shared state stays under lock
        self.assertTrue((Path(w.workdir) / "src" / "a.py").exists())  # copy still done
        self.assertFalse((self.db / "shared").exists())    # setup NOT pre-run
        w.teardown()

    def test_gate_consumes_warmup_and_passes(self):
        cfg = self._cfg(isolated=True)
        w = GateWarmup(cfg, timeout=30).start()
        res = run_remote_gate(cfg, {"src/a.py": b"V = 42\n"}, timeout=30, warmup=w)
        self.assertTrue(res.passed, f"exit={res.exit_code} tail={res.output_tail!r}")
        self.assertFalse(any(self.db.iterdir()), "per-{id} state must be torn down")
        self.assertFalse(Path(w.workdir).exists())

    def test_gate_falls_back_on_failed_warmup(self):
        cfg = self._cfg(isolated=True)
        dead = GateWarmup(cfg, timeout=30)
        dead.error = "forced warmup failure"               # never staged
        res = run_remote_gate(cfg, {"src/a.py": b"V = 42\n"}, timeout=30, warmup=dead)
        self.assertTrue(res.passed, f"exit={res.exit_code} tail={res.output_tail!r}")
        self.assertFalse(any(self.db.iterdir()))

    def test_unconsumed_warmup_leaves_no_orphan(self):
        cfg = self._cfg(isolated=True)
        w = GateWarmup(cfg, timeout=30).start()
        w.join()
        run_id = w.run_id
        w.teardown()                                       # worker produced nothing
        self.assertFalse((self.db / f"d_{run_id}").exists())
        self.assertFalse(Path(w.workdir).exists())


class WarmupStillStagingIsNotAdopted(unittest.TestCase):
    """#1: join() bounded the wait but _stage makes TWO remote calls of
    `timeout` each, so the thread can outlive the join. `error` is None while
    staging is merely UNFINISHED, so the old adoption test (`error is None`)
    read "still copying" as "ready" — the gate then overlaid a half-copied tree
    and re-ran setup concurrently with the warmup's own."""

    def setUp(self):
        RG._ssh_base = _fake_ssh_base
        RG.subprocess.run = _patched_run
        self.warm = _warm_checkout()
        self.wbase = Path(mkdtemp())

    def tearDown(self):
        RG.subprocess.run = _real_run

    def _cfg(self):
        cfg = parse_remote_config({
            "ssh": "root@host", "repo_dir": str(self.warm),
            "copy_cmd": "cp -R {repo} {workdir}",
            "test_cmd": "true",
            "workdir_base": str(self.wbase),
        }, "python")
        assert cfg is not None
        return cfg

    def test_join_reports_incomplete_and_workdir_is_not_adopted(self):
        cfg = self._cfg()
        release = threading.Event()
        w = GateWarmup(cfg, timeout=30)

        def _slow_stage():
            release.wait(10)          # stands in for a copy still in flight
            w._done.set()

        w._thread = threading.Thread(target=_slow_stage, daemon=True)
        w._deadline = time.monotonic() + 0.2      # join gives up almost at once
        w._thread.start()
        try:
            self.assertFalse(w.join(), "join must report staging as incomplete")
            self.assertIsNone(w.error, "an unfinished warmup has no error yet")
            self.assertFalse(
                _adoptable(w),
                "a warmup whose staging thread is still live must not be adopted",
            )
        finally:
            release.set()
            w._thread.join(5)
        # ...and the same predicate does say yes once staging finishes, so the
        # assertion above is about liveness, not a blanket refusal.
        self.assertTrue(w.join())
        self.assertTrue(_adoptable(w))

    def test_teardown_of_a_live_warmup_does_not_block_the_gate(self):
        """teardown() used to join the staging thread inline, so discarding an
        unfinished warmup stalled the gate for the whole timeout."""
        cfg = self._cfg()
        release = threading.Event()
        w = GateWarmup(cfg, timeout=30)

        def _slow_stage():
            release.wait(10)
            w._done.set()

        w._thread = threading.Thread(target=_slow_stage, daemon=True)
        w._deadline = time.monotonic() + 30
        w._thread.start()
        try:
            started = time.monotonic()
            w.teardown()
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            release.set()
            w._thread.join(5)


if __name__ == "__main__":
    unittest.main()
