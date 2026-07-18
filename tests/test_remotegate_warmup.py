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
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev import remotegate as RG  # noqa: E402
from shepherd_dev.remotegate import GateWarmup, parse_remote_config, run_remote_gate  # noqa: E402

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
    warm = Path(tempfile.mkdtemp())
    (warm / "src").mkdir()
    (warm / "src" / "a.py").write_text("V = 1\n")
    return warm


class RemoteGateWarmup(unittest.TestCase):
    def setUp(self):
        from shepherd_dev import procstream as PS

        RG._ssh_base = _fake_ssh_base
        RG.subprocess.run = _patched_run
        PS.subprocess.Popen = _patched_popen
        self.db = Path(tempfile.mkdtemp())          # stands in for a DB/service store
        self.warm = _warm_checkout()
        self.wbase = Path(tempfile.mkdtemp())

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


if __name__ == "__main__":
    unittest.main()
