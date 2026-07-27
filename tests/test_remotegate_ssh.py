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

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev import remotegate as RG  # noqa: E402
from shepherd_dev.remotegate import (  # noqa: E402
    _build_test_line,
    _remote_argv,
    parse_remote_config,
    run_remote_gate,
)

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
        warm = Path(mkdtemp())
        (warm / "src").mkdir()
        cfg = parse_remote_config({"ssh": "root@host", "repo_dir": str(warm), "test_cmd": "true"}, None)
        assert cfg is not None
        self.assertIsNone(RG.preflight(cfg))  # buggy code returned a false "repo_dir missing"

    def test_preflight_fails_for_missing_repo(self):
        cfg = parse_remote_config({"ssh": "root@host", "repo_dir": "/nope-xyz-123", "test_cmd": "true"}, None)
        assert cfg is not None
        self.assertIsNotNone(RG.preflight(cfg))

    def test_preflight_looks_past_an_env_prefix(self):
        """#9: `MIX_ENV=test mix test` is a normal test_cmd. Taking argv[0]
        blindly made the binary check `command -v MIX_ENV=test`, which can
        never resolve — preflight rejected a perfectly good remote config."""
        warm = Path(mkdtemp())
        # a binary guaranteed absent, so the check has to RUN and name it
        for test_cmd, missing in (
            ("MIX_ENV=test nope-xyz-123 test", "nope-xyz-123"),
            ("RAILS_ENV=test CI=1 nope-xyz-456 exec rspec", "nope-xyz-456"),
        ):
            with self.subTest(test_cmd=test_cmd):
                cfg = parse_remote_config(
                    {"ssh": "root@host", "repo_dir": str(warm), "test_cmd": test_cmd}, None
                )
                assert cfg is not None
                err = RG.preflight(cfg)
                self.assertIsNotNone(err)
                self.assertIn(missing, err or "")       # the real command word
                self.assertNotIn("_ENV=test", err or "")  # never the assignment

    def test_preflight_accepts_a_real_binary_behind_an_env_prefix(self):
        warm = Path(mkdtemp())
        cfg = parse_remote_config(
            {"ssh": "root@host", "repo_dir": str(warm), "test_cmd": "MIX_ENV=test sh -c true"},
            None,
        )
        assert cfg is not None
        self.assertIsNone(RG.preflight(cfg))

    def test_preflight_still_checks_the_binary_after_a_prefix(self):
        warm = Path(mkdtemp())
        cfg = parse_remote_config(
            {"ssh": "root@host", "repo_dir": str(warm), "test_cmd": "FOO=bar true"}, None
        )
        assert cfg is not None
        self.assertIsNone(RG.preflight(cfg))  # `true` does exist

    def test_preflight_skips_the_check_for_an_all_assignment_command(self):
        warm = Path(mkdtemp())
        cfg = parse_remote_config(
            {"ssh": "root@host", "repo_dir": str(warm), "test_cmd": "FOO=bar"}, None
        )
        assert cfg is not None
        self.assertIsNone(RG.preflight(cfg))  # nothing to resolve, do not invent one

    def test_the_test_step_does_not_require_gnu_timeout(self):
        """`timeout` is GNU coreutils: absent on macOS and minimal images.
        Invoking it unconditionally made every gate on such a host exit 127
        with `timeout: command not found`, read as an ordinary suite failure.

        Asserted on the command itself. Emptying PATH to prove it end-to-end
        would take the copy, overlay and teardown steps down with it, and test
        those rather than this."""
        line = _build_test_line("/w", "", "pytest -q", 600)
        self.assertIn("command -v timeout", line)
        self.assertIn("timeout 600 pytest -q", line)
        self.assertIn("gtimeout 600 pytest -q", line)  # coreutils under brew
        self.assertTrue(line.rstrip().endswith("else pytest -q; fi"))

    def test_the_fallback_chain_actually_runs_under_each_shape(self):
        """Each branch must be a runnable command, not a string that looks
        right — and each must be the branch actually taken. A stub that only
        forwards would pass whichever branch ran, so it prints a marker.

        PATH holds nothing but the stub dir plus a symlinked `sh`, so
        "unavailable" really means unavailable: pointing at /usr/bin would find
        the system timeout on Linux and silently test the wrong branch."""
        import subprocess as real_sub

        for available in (True, False):
            with self.subTest(timeout_available=available):
                stub = Path(mkdtemp())
                (stub / "sh").symlink_to("/bin/sh")
                (stub / "echo").write_text('#!/bin/sh\n/bin/echo "$@"\n')
                (stub / "echo").chmod(0o755)
                if available:
                    (stub / "timeout").write_text(
                        '#!/bin/sh\n/bin/echo VIA_TIMEOUT\nshift\nexec "$@"\n'
                    )
                    (stub / "timeout").chmod(0o755)
                line = _build_test_line(".", "", "echo GATE_OK", 5)
                proc = real_sub.run(
                    ["/bin/sh", "-c", line], capture_output=True, text=True,
                    env={"PATH": str(stub)},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("GATE_OK", proc.stdout)
                self.assertNotIn("not found", proc.stderr)
                # the branch that ran is the branch under test
                self.assertEqual(available, "VIA_TIMEOUT" in proc.stdout)

    def test_suite_exiting_124_quickly_is_a_gate_failure_not_a_timeout(self):
        """#10: exit 124 was read as "timeout(1) killed it" unconditionally, so
        a suite that exits 124 on its own became an infra_error — which aborts
        the whole run instead of counting as a retryable gate failure."""
        warm = Path(mkdtemp())
        (warm / "src").mkdir()
        cfg = parse_remote_config({
            "ssh": "root@host", "repo_dir": str(warm),
            "copy_cmd": "cp -R {repo} {workdir}",
            "test_cmd": "sh -c 'exit 124'",
            "workdir_base": str(Path(mkdtemp())),
        }, "python")
        assert cfg is not None
        res = run_remote_gate(cfg, {"src/a.py": b"V = 1\n"}, timeout=30)
        self.assertFalse(res.passed)
        self.assertEqual(res.exit_code, 124)
        self.assertIsNone(res.infra_error, "an immediate 124 is the suite's own verdict")

    def test_preflight_warns_when_the_remote_has_no_timeout(self):
        """Not fatal — the gate runs the suite bare — but the budget is then
        enforced only by the local ssh deadline, which the user should hear.

        Driven by what the remote REPORTS rather than by editing PATH: the
        probe runs on the remote, and stripping the local PATH only breaks the
        harness's own `bash -lc`."""
        warm = Path(mkdtemp())
        cfg = parse_remote_config(
            {"ssh": "root@host", "repo_dir": str(warm), "test_cmd": "true"}, None
        )
        assert cfg is not None

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = "SHEPHERD_NO_REMOTE_TIMEOUT\n"

        sent = {}

        def fake_remote(cfg_, script, timeout_):
            sent["script"] = script
            return _Proc()

        old = RG._remote
        RG._remote = fake_remote
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                self.assertIsNone(RG.preflight(cfg))  # a warning, never a failure
        finally:
            RG._remote = old

        self.assertIn("command -v timeout", sent["script"])  # the probe is sent
        self.assertIn("gtimeout", sent["script"])
        self.assertIn("no timeout(1)", err.getvalue())

    def test_preflight_stays_quiet_when_the_remote_has_timeout(self):
        """Stubbed like the case above. Letting the real probe run would make
        the assertion depend on whether THIS machine ships timeout(1) — which
        is the very platform difference under test."""
        warm = Path(mkdtemp())
        cfg = parse_remote_config(
            {"ssh": "root@host", "repo_dir": str(warm), "test_cmd": "true"}, None
        )
        assert cfg is not None

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""  # the probe found one, so it said nothing

        old = RG._remote
        RG._remote = lambda cfg_, script, timeout_: _Proc()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                self.assertIsNone(RG.preflight(cfg))
        finally:
            RG._remote = old
        self.assertNotIn("no timeout(1)", err.getvalue())

    def test_a_real_remote_timeout_is_still_infra(self):
        warm = Path(mkdtemp())
        (warm / "src").mkdir()
        cfg = parse_remote_config({
            "ssh": "root@host", "repo_dir": str(warm),
            "copy_cmd": "cp -R {repo} {workdir}",
            "test_cmd": "sleep 30",
            "workdir_base": str(Path(mkdtemp())),
        }, "python")
        assert cfg is not None
        # This asserts the REMOTE kill, so the remote needs a timeout(1) to do
        # it with. macOS has none, and without one `sleep 30` simply finishes
        # inside the local deadline and the gate passes — correct behaviour,
        # but not what this test is about. Put a shim first on PATH.
        shim = Path(mkdtemp())
        (shim / "timeout").write_text(
            '#!/bin/sh\nlimit=$1; shift\n"$@" & pid=$!\n'
            '( sleep "$limit"; kill -9 $pid 2>/dev/null ) & watcher=$!\n'
            'wait $pid; rc=$?\nkill $watcher 2>/dev/null\n'
            '[ $rc -ge 128 ] && exit 124\nexit $rc\n'
        )
        (shim / "timeout").chmod(0o755)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{shim}{os.pathsep}{old_path}"
        try:
            res = run_remote_gate(cfg, {"src/a.py": b"V = 1\n"}, timeout=1)
        finally:
            os.environ["PATH"] = old_path
        self.assertFalse(res.passed)
        self.assertIsNotNone(res.infra_error)
        self.assertIn("timed out", res.infra_error or "")

    def test_full_gate_with_spaces_and_operators(self):
        warm = Path(mkdtemp())
        (warm / "src").mkdir()
        (warm / "src" / "a.py").write_text("V=1\n")
        db = Path(mkdtemp())
        cfg = parse_remote_config({
            "ssh": "root@host", "repo_dir": str(warm), "copy_cmd": "cp -R {repo} {workdir}",
            "setup_cmd": f"mkdir -p {db}/d_{{id}} && echo up > {db}/d_{{id}}/s",
            "test_cmd": f"test \"$(cat {db}/d_{{id}}/s)\" = up && grep -q 'V = 42' src/a.py && echo GATE_OK",
            "teardown_cmd": f"rm -rf {db}/d_{{id}}",
            "workdir_base": mkdtemp(),
        }, "python")
        assert cfg is not None
        res = run_remote_gate(cfg, {"src/a.py": b"V = 42\n"}, timeout=30)
        self.assertTrue(res.passed, f"gate should pass; exit={res.exit_code} tail={res.output_tail!r}")
        self.assertEqual(res.exit_code, 0)
        self.assertFalse(any(db.iterdir()), "teardown must remove the per-{id} state")


if __name__ == "__main__":
    unittest.main()
