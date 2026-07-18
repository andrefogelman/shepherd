"""Regression tests for the remote-gate SSH invocation.

The bug: passing ["ssh", host, "bash", "-lc", script] as separate argv looks
correct locally but breaks over real ssh — ssh JOINS every arg after the host
into one remote command string, which the remote login shell RE-TOKENIZES, so
`bash -c` receives only the first word of the script. Any script with spaces or
operators (all of them) is mis-parsed → preflight/setup/test/teardown all fail.

These tests use a faithful fake-ssh that reproduces that join + re-tokenization
(so they FAIL against the buggy code and PASS against the fix), plus the
substitution helpers. Runnable with: python -m unittest tests.test_remotegate_ssh
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev import remotegate as RG  # noqa: E402
from shepherd_dev.remotegate import parse_remote_config, run_remote_gate, _remote_argv  # noqa: E402

_real_run = subprocess.run


def _fake_ssh_base(_cfg):
    # Sentinel first token; the patched run() detects it.
    return ["__FAKESSH__"]


def _patched_run(argv, **kw):
    """Model real ssh: join every arg after the host into ONE string and let the
    remote shell re-tokenize it (sh -c re-splits on spaces, exactly as a remote
    login shell would)."""
    if isinstance(argv, list) and argv and argv[0] == "__FAKESSH__":
        remote_cmd = " ".join(argv[1:])
        return _real_run(["sh", "-c", remote_cmd], **kw)
    return _real_run(argv, **kw)


_real_popen = subprocess.Popen


def _patched_popen(argv, **kw):
    # The streamed remote test step (procstream) launches via Popen; same
    # real-ssh join + re-tokenization semantics as _patched_run.
    if isinstance(argv, list) and argv and argv[0] == "__FAKESSH__":
        return _real_popen(["sh", "-c", " ".join(argv[1:])], **kw)
    return _real_popen(argv, **kw)


class RemoteGateSSHQuoting(unittest.TestCase):
    def setUp(self):
        from shepherd_dev import procstream as PS

        RG._ssh_base = _fake_ssh_base
        RG.subprocess.run = _patched_run
        PS.subprocess.Popen = _patched_popen

    def tearDown(self):
        from shepherd_dev import procstream as PS

        RG.subprocess.run = _real_run
        PS.subprocess.Popen = _real_popen

    def test_remote_argv_is_single_quoted_arg(self):
        cfg = parse_remote_config({"ssh": "root@host", "repo_dir": "/x", "test_cmd": "true"}, None)
        assert cfg is not None
        argv = _remote_argv(cfg, "test -d /x && echo Y || echo N")
        # exactly one remote arg after the ssh base, and it wraps the whole script
        self.assertTrue(argv[-1].startswith("bash -lc "))
        self.assertIn("test -d /x && echo Y", argv[-1])

    def test_preflight_passes_for_existing_repo(self):
        warm = Path(tempfile.mkdtemp())
        (warm / "src").mkdir()
        cfg = parse_remote_config({"ssh": "root@host", "repo_dir": str(warm), "test_cmd": "true"}, None)
        assert cfg is not None
        self.assertIsNone(RG.preflight(cfg))  # buggy code returned a false "repo_dir missing"

    def test_preflight_fails_for_missing_repo(self):
        cfg = parse_remote_config({"ssh": "root@host", "repo_dir": "/nope-xyz-123", "test_cmd": "true"}, None)
        assert cfg is not None
        self.assertIsNotNone(RG.preflight(cfg))

    def test_full_gate_with_spaces_and_operators(self):
        warm = Path(tempfile.mkdtemp())
        (warm / "src").mkdir()
        (warm / "src" / "a.py").write_text("V=1\n")
        db = Path(tempfile.mkdtemp())
        cfg = parse_remote_config({
            "ssh": "root@host", "repo_dir": str(warm), "copy_cmd": "cp -R {repo} {workdir}",
            "setup_cmd": f"mkdir -p {db}/d_{{id}} && echo up > {db}/d_{{id}}/s",
            "test_cmd": f"test \"$(cat {db}/d_{{id}}/s)\" = up && grep -q 'V = 42' src/a.py && echo GATE_OK",
            "teardown_cmd": f"rm -rf {db}/d_{{id}}",
            "workdir_base": tempfile.mkdtemp(),
        }, "python")
        assert cfg is not None
        res = run_remote_gate(cfg, {"src/a.py": b"V = 42\n"}, timeout=30)
        self.assertTrue(res.passed, f"gate should pass; exit={res.exit_code} tail={res.output_tail!r}")
        self.assertEqual(res.exit_code, 0)
        self.assertFalse(any(db.iterdir()), "teardown must remove the per-{id} state")


if __name__ == "__main__":
    unittest.main()
