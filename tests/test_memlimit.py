"""The opt-in memory ceiling (limits.memory_max / --memory-max).

A worker's own test grew to 19.8 GB on a shared host and the kernel's global
OOM killer had to choose among every process on the machine. With a limit set,
each worker, reviewer and local gate launch runs in its own memory-capped
cgroup scope; a limit that cannot be enforced stops the run. With none set,
every argv is exactly what it was. Runnable with:
    python -m unittest tests.test_memlimit
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev import memlimit  # noqa: E402

GIB = 1024 ** 3


class _Reset(unittest.TestCase):
    def setUp(self):
        self._saved = memlimit._active
        memlimit._active = None

    def tearDown(self):
        memlimit._active = self._saved


class ParseSize(unittest.TestCase):
    def test_units(self):
        self.assertEqual(memlimit.parse_size("8G"), 8 * GIB)
        self.assertEqual(memlimit.parse_size("512M"), 512 * 1024 ** 2)
        self.assertEqual(memlimit.parse_size("8GiB"), 8 * GIB)
        self.assertEqual(memlimit.parse_size(" 1t "), 1024 ** 4)
        self.assertEqual(memlimit.parse_size(2 * GIB), 2 * GIB)
        self.assertEqual(memlimit.parse_size(str(GIB)), GIB)

    def test_refused(self):
        for bad in ("", "lots", "8X", "-1G", "1.5G", True, "100M", 1024):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                memlimit.parse_size(bad)


class ScopeArgv(unittest.TestCase):
    def test_shape(self):
        argv = memlimit.scope_argv(2 * GIB, "/run/user/7")
        self.assertEqual(argv[:2], ["env", "XDG_RUNTIME_DIR=/run/user/7"])
        self.assertEqual(argv[2:5], ["systemd-run", "--user", "--scope"])
        self.assertIn("--collect", argv)
        self.assertIn(f"MemoryMax={2 * GIB}", argv)
        self.assertIn("MemorySwapMax=0", argv)  # no swap: the cap is the cap
        self.assertEqual(argv[-1], "--")
        self.assertNotIn("--expand-environment=no", argv)

    def test_expansion_off(self):
        # systemd-run >= 254 expands ${...} and $$ in the command it runs; the
        # command is a worker prompt or a gate string, which must arrive intact
        argv = memlimit.scope_argv(2 * GIB, "/run/user/7", no_expand=True)
        self.assertLess(argv.index("--expand-environment=no"), argv.index("--"))


def _proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


def _versioned(run, version="systemd 255 (255.4-1ubuntu8.18)"):
    """`run` for the scope probe, answering `systemd-run --version` first."""
    def dispatch(argv, **kw):
        if argv[-1] == "--version":
            return _proc(0, out=f"{version}\n+PAM +AUDIT\n")
        return run(argv, **kw)
    return dispatch


class UnavailableReason(unittest.TestCase):
    def _linux(self, run, which="/usr/bin/systemd-run", rundir="/run/user/7", version="systemd 255 (255.4)"):
        with mock.patch.object(memlimit.sys, "platform", "linux"), \
                mock.patch.object(memlimit.shutil, "which", lambda _n: which), \
                mock.patch.object(memlimit, "runtime_dir", lambda: rundir):
            return memlimit.unavailable_reason(GIB, run=run if run is None else _versioned(run, version))

    def test_not_linux(self):
        with mock.patch.object(memlimit.sys, "platform", "darwin"):
            self.assertIn("Linux", memlimit.unavailable_reason(GIB, run=None) or "")

    def test_no_systemd_run(self):
        self.assertIn("systemd-run", self._linux(None, which=None) or "")

    def test_no_user_manager(self):
        self.assertIn("runtime directory", self._linux(None, rundir=None) or "")

    def test_scope_fails(self):
        reason = self._linux(lambda *a, **k: _proc(1, err="Failed to connect to bus: No medium found\n"))
        self.assertIn("No medium found", reason or "")

    def test_controller_not_delegated(self):
        # a scope that starts but carries no limit is the case the read-back exists for
        reason = self._linux(lambda *a, **k: _proc(0, out="max\n"))
        self.assertIn("not delegated", reason or "")

    def test_enforceable(self):
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            return _proc(0, out=f"{GIB}\n")

        self.assertIsNone(self._linux(run))
        self.assertEqual(seen["argv"][:3], ["env", "XDG_RUNTIME_DIR=/run/user/7", "systemd-run"])
        self.assertIn("--expand-environment=no", seen["argv"])

    def test_old_systemd_gets_no_expansion_flag(self):
        # before 254 systemd-run does not expand, and rejects the flag
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            return _proc(0, out=f"{GIB}\n")

        self.assertIsNone(self._linux(run, version="systemd 252 (252.22-1~deb12u1)"))
        self.assertNotIn("--expand-environment=no", seen["argv"])

    def test_unreadable_version_is_refused(self):
        # without the version there is no knowing whether the command would be rewritten
        reason = self._linux(lambda *a, **k: _proc(0, out=f"{GIB}\n"), version="something else")
        self.assertIn("version", reason or "")


class ActivateAndWrap(_Reset):
    def test_failed_activation_leaves_nothing_active(self):
        with mock.patch.object(memlimit, "unavailable_reason", lambda *a, **k: "no"):
            self.assertEqual(memlimit.activate(GIB), "no")
        self.assertIsNone(memlimit.active())

    def test_activation_and_reset(self):
        with mock.patch.object(memlimit, "unavailable_reason", lambda *a, **k: None):
            self.assertIsNone(memlimit.activate(GIB))
        self.assertEqual(memlimit.active(), GIB)
        memlimit.activate(None)
        self.assertIsNone(memlimit.active())

    def test_inactive_wrap_is_identity(self):
        argv = ["perl", "-e", "x"]
        self.assertIs(memlimit.wrap_argv(argv), argv)
        self.assertEqual(memlimit.wrap_shell("python3 -m unittest -q"), "python3 -m unittest -q")

    def test_active_wrap(self):
        memlimit._active = GIB
        with mock.patch.object(memlimit, "runtime_dir", lambda: "/run/user/7"):
            argv = memlimit.wrap_argv(["perl", "-e", "x"])
            shell = memlimit.wrap_shell("python3 -m unittest -q 2>&1 | tail -1")
        self.assertEqual(argv[-3:], ["perl", "-e", "x"])
        self.assertIn(f"MemoryMax={GIB}", argv)
        self.assertEqual(shlex.split(shell)[-3:], ["sh", "-c", "python3 -m unittest -q 2>&1 | tail -1"])

    def test_command_gets_back_the_runtime_dir_it_had(self):
        # systemd-run needs XDG_RUNTIME_DIR; the command must not inherit it
        # from the wrap, or a jailed worker is handed the user bus's path
        memlimit._active = GIB
        with mock.patch.object(memlimit, "runtime_dir", lambda: "/run/user/7"):
            for parent, tail in (
                (None, ["env", "-u", "XDG_RUNTIME_DIR"]),
                ("/run/user/7", []),
                ("/elsewhere", ["env", "XDG_RUNTIME_DIR=/elsewhere"]),
            ):
                with self.subTest(parent=parent):
                    env = {} if parent is None else {"XDG_RUNTIME_DIR": parent}
                    with mock.patch.dict(memlimit.os.environ, env, clear=True):
                        argv = memlimit.wrap_argv(["cmd"])
                    cut = argv.index("--") + 1
                    self.assertEqual(argv[cut:], [*tail, "cmd"])

    def test_activation_turns_expansion_off_where_systemd_expands(self):
        for version, flagged in (("systemd 255 (255.4)", True), ("systemd 252 (252.22)", False)):
            with self.subTest(version=version):
                with mock.patch.object(memlimit, "unavailable_reason", lambda *a, **k: None):
                    memlimit.activate(GIB, run=_versioned(None, version))
                with mock.patch.object(memlimit, "runtime_dir", lambda: "/run/user/7"):
                    argv = memlimit.wrap_argv(["cmd"])
                self.assertEqual("--expand-environment=no" in argv, flagged)

    def test_active_wrap_without_runtime_dir_refuses(self):
        memlimit._active = GIB
        with mock.patch.object(memlimit, "runtime_dir", lambda: None), self.assertRaises(RuntimeError):
            memlimit.wrap_argv(["true"])


class LocalGate(_Reset):
    """The local gate's test command goes through the scope; without a limit
    it is the command it always was."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-memgate-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.py").write_text("V = 1\n")

    def _gate_cmd(self) -> str:
        from shepherd_dev import procstream
        from shepherd_dev.supervisor import _run_gate

        seen = {}

        def fake(cmd, **kw):
            seen["cmd"] = cmd
            return SimpleNamespace(timed_out=False, output="ok\n", returncode=0)

        with mock.patch.object(procstream, "run_streaming", fake):
            res = _run_gate(self.repo, {"src/a.py": b"V = 2\n"}, "echo ok", timeout=30)
        self.assertTrue(res.passed)
        return seen["cmd"]

    def test_no_limit_leaves_the_command_alone(self):
        self.assertEqual(self._gate_cmd(), "echo ok")

    def test_limit_wraps_the_command(self):
        memlimit._active = GIB
        with mock.patch.object(memlimit, "runtime_dir", lambda: "/run/user/7"):
            cmd = self._gate_cmd()
        parts = shlex.split(cmd)
        self.assertIn("systemd-run", parts)
        self.assertIn(f"MemoryMax={GIB}", parts)
        self.assertEqual(parts[-3:], ["sh", "-c", "echo ok"])

    def _pre_gate_cmd(self) -> str:
        from shepherd_dev import supervisor

        seen = {}

        def fake(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(supervisor.subprocess, "run", fake):
            _entries, error = supervisor.run_pre_gate(self.repo, {"src/a.py": b"V = 2\n"}, "black .")
        self.assertIsNone(error)
        return seen["cmd"]

    def test_pre_gate_without_a_limit_is_unchanged(self):
        self.assertEqual(self._pre_gate_cmd(), "black .")

    def test_pre_gate_runs_inside_the_scope(self):
        # the fixer runs over the worker's proposal; `npm run lint` can execute repo code
        memlimit._active = GIB
        with mock.patch.object(memlimit, "runtime_dir", lambda: "/run/user/7"):
            parts = shlex.split(self._pre_gate_cmd())
        self.assertIn(f"MemoryMax={GIB}", parts)
        self.assertEqual(parts[-3:], ["sh", "-c", "black ."])


try:
    from shepherd_dialect.workspace_control import runtime_provider as _rp

    _HAS_SUBSTRATE = True
except Exception:  # pragma: no cover - substrate absent
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class WorkerArgv(_Reset):
    """Through the real transport: the scope is outermost, and the perl
    killtree script is still where the swap put it."""

    def setUp(self):
        super().setUp()
        self._previous = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS

    def tearDown(self):
        _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = self._previous
        super().tearDown()

    def _argv(self):
        from shepherd_dev.launch import LaunchPolicy
        from shepherd_dev.supervisor import set_worker_budget

        self.assertTrue(set_worker_budget(300, launch=LaunchPolicy.from_config(None, {})))
        invocation = SimpleNamespace(
            provider_id="claude", prompt="p", model_name=None,
            task_lock=SimpleNamespace(task_id="x.y"), kwargs={},
        )
        provider = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude(invocation)
        return [str(a) for a in provider.command_argv("/w", "/usr/local/bin/claude")]

    def test_no_limit_starts_with_perl(self):
        from shepherd_dev.supervisor import _KILLTREE_PERL

        argv = self._argv()
        self.assertTrue(argv[0].endswith("perl"))
        self.assertEqual(argv[2], _KILLTREE_PERL)

    def test_limit_wraps_the_whole_launch(self):
        from shepherd_dev.supervisor import _KILLTREE_PERL

        memlimit._active = GIB
        with mock.patch.object(memlimit, "runtime_dir", lambda: "/run/user/7"), \
                mock.patch.dict(memlimit.os.environ):
            memlimit.os.environ.pop("XDG_RUNTIME_DIR", None)
            argv = self._argv()
        cut = argv.index("--") + 1
        self.assertIn(f"MemoryMax={GIB}", argv[:cut])
        # the parent had no XDG_RUNTIME_DIR, so neither does the worker
        self.assertEqual(argv[cut:cut + 3], ["env", "-u", "XDG_RUNTIME_DIR"])
        self.assertTrue(argv[cut + 3].endswith("perl"))
        self.assertEqual(argv[cut + 5], _KILLTREE_PERL)


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class PlainProviderFallback(_Reset):
    """When our provider cannot be built the framework's own one launches,
    and its argv is never wrapped: with a limit active it must not launch."""

    def test_no_limit_falls_back(self):
        from shepherd_dialect import providers

        from shepherd_dev.supervisor import _plain_provider

        sentinel = object()
        with mock.patch.object(providers, "ClaudeHeadlessProvider", lambda **kw: sentinel):
            self.assertIs(_plain_provider(providers, {}), sentinel)

    def test_limit_refuses_to_fall_back(self):
        from shepherd_dialect import providers

        from shepherd_dev.supervisor import _plain_provider

        memlimit._active = GIB
        with self.assertRaises(RuntimeError):
            _plain_provider(providers, {})


class Cli(_Reset):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-memcli-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self._home = mock.patch("shepherd_dev.config.load_global_config", lambda: {})
        self._home.start()
        self.addCleanup(self._home.stop)

    def _apply(self, flag=None, config_value=None, provider="claude", reason=None):
        from shepherd_dev import cli

        if config_value is not None:
            (self.repo / ".shepherd-dev.json").write_text(json.dumps({"limits": {"memory_max": config_value}}))
        args = types.SimpleNamespace(memory_max=flag, provider=provider)
        with mock.patch.object(memlimit, "unavailable_reason", lambda *a, **k: reason):
            return cli._apply_memory_limit(args, self.repo)

    def test_nothing_set_runs_without_a_limit(self):
        memlimit._active = GIB  # a previous run's limit must not leak into this one
        self.assertTrue(self._apply())
        self.assertIsNone(memlimit.active())

    def test_flag_beats_config(self):
        self.assertTrue(self._apply(flag="2G", config_value="8G"))
        self.assertEqual(memlimit.active(), 2 * GIB)

    def test_config_alone(self):
        self.assertTrue(self._apply(config_value="8G"))
        self.assertEqual(memlimit.active(), 8 * GIB)

    def test_unreadable_config_stops_the_run(self):
        self.assertFalse(self._apply(config_value="lots"))
        self.assertIsNone(memlimit.active())

    def test_unenforceable_stops_the_run(self):
        self.assertFalse(self._apply(flag="8G", reason="no user manager"))
        self.assertIsNone(memlimit.active())

    def test_hosted_providers_are_refused(self):
        for provider in ("codex", "grok"):
            with self.subTest(provider=provider):
                self.assertFalse(self._apply(flag="8G", provider=provider))
                self.assertIsNone(memlimit.active())

    def test_failed_rebind_stops_a_limited_run(self):
        # the worker and reviewer argv is wrapped only by the rebound provider;
        # without it they would run uncapped after the CLI said the limit was on
        from shepherd_dev import cli

        for limit, bound, expected in ((GIB, False, False), (GIB, True, True), (None, False, True)):
            with self.subTest(limit=limit, bound=bound):
                memlimit._active = limit
                with mock.patch.object(cli, "set_worker_budget", lambda *a, **k: bound):
                    self.assertEqual(cli._bind_claude_launches(300), expected)

    def test_flag_validator(self):
        import argparse

        from shepherd_dev.cli import _memory_size

        self.assertEqual(_memory_size("8G"), "8G")
        with self.assertRaises(argparse.ArgumentTypeError):
            _memory_size("8X")


@unittest.skipUnless(
    sys.platform == "linux" and memlimit.unavailable_reason(512 * 1024 ** 2) is None,
    "needs a Linux user manager with the memory controller delegated",
)
class Live(_Reset):
    """On a host that can enforce it: an allocation past the cap dies inside
    the scope, one under it runs."""

    def _run(self, nbytes: int) -> subprocess.CompletedProcess:
        memlimit._active = 512 * 1024 ** 2
        cmd = memlimit.wrap_shell(f"{shlex.quote(sys.executable)} -c 'x = bytearray({nbytes}); print(1)'")
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)

    def test_over_the_cap_is_killed(self):
        self.assertNotEqual(self._run(1536 * 1024 ** 2).returncode, 0)

    def test_under_the_cap_runs(self):
        res = self._run(64 * 1024 ** 2)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
