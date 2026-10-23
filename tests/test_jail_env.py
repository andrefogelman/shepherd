"""`jail_env`: repo-declared environment the worker needs to check its own work.

The worker's jail is materialized from a git tree, so everything gitignored —
deps/, _build/, node_modules/, target/, .venv/ — is absent by construction. A
compiled language's worker therefore cannot compile, and a class of error that
`mix compile` reports in seconds costs a full 13-minute attempt plus a gate
instead. Measured on a real Phoenix repo: a git-archive checkout (13M, no
deps/, no _build/) compiles in 42s when MIX_DEPS_PATH points at a cache
outside it, and the cache is only read — zero files touched.

The substrate offers no per-run env hook (RuntimeOptions carries trace,
provider and model, nothing else), so the values travel through the process
environment the provider's child inherits.

Runnable with: python -m unittest tests.test_jail_env
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hostprograms import fresh_programs  # noqa: E402


class _Repo:
    """A repo root carrying a .shepherd-dev.json."""

    def __init__(self, config):
        from tmpdirs import mkdtemp

        self.root = Path(mkdtemp(prefix="shepherd-jailenv-"))
        if config is not None:
            (self.root / ".shepherd-dev.json").write_text(
                json.dumps(config), encoding="utf-8"
            )


class JailEnvConfigTests(unittest.TestCase):
    def setUp(self):
        fresh_programs(self)

    def test_absent_config_is_an_empty_mapping_not_an_error(self):
        from shepherd_dev.config import jail_env

        self.assertEqual(jail_env(_Repo(None).root), {})
        self.assertEqual(jail_env(_Repo({"test_cmd": "mix test"}).root), {})

    def test_declared_values_come_back_verbatim(self):
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {"MIX_BUILD_ROOT": ".claude-scratch/build"}})
        self.assertEqual(jail_env(repo.root), {"MIX_BUILD_ROOT": ".claude-scratch/build"})

    def test_a_leading_tilde_is_expanded_because_the_child_shell_will_not(self):
        """The value reaches the worker as an environment variable, not as
        shell input — nothing expands `~` on the way."""
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "~/.cache/sac-deps"}})
        got = jail_env(repo.root)["MIX_DEPS_PATH"]
        self.assertTrue(got.startswith(str(Path.home())), got)
        self.assertNotIn("~", got)

    def test_a_relative_path_stays_relative(self):
        """MIX_BUILD_ROOT=.claude-scratch/build means "inside the clone", and
        the clone is the worker's cwd. Resolving it here would bind it to
        whatever directory the supervisor happened to run from."""
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {"MIX_BUILD_ROOT": ".claude-scratch/build"}})
        self.assertEqual(jail_env(repo.root)["MIX_BUILD_ROOT"], ".claude-scratch/build")

    def test_non_string_entries_are_dropped_rather_than_coerced(self):
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {"GOOD": "1", "BAD": 2, "ALSO_BAD": None}})
        self.assertEqual(jail_env(repo.root), {"GOOD": "1"})

    def test_a_non_object_jail_env_is_ignored(self):
        from shepherd_dev.config import jail_env

        for bad in ("MIX_DEPS_PATH=x", ["MIX_DEPS_PATH"], 3):
            with self.subTest(value=bad):
                self.assertEqual(jail_env(_Repo({"jail_env": bad}).root), {})

    def test_variables_that_would_redirect_our_own_interpreter_are_refused(self):
        """CHILD_ENV_STRIP exists because a wrong PYTHONHOME stops the
        interpreter booting before any of our code runs. A repo config must
        not be able to reintroduce exactly what we scrub."""
        from shepherd_dev.config import jail_env

        repo = _Repo({
            "jail_env": {
                "PYTHONPATH": "/evil",
                "PYTHONHOME": "/evil",
                "VIRTUAL_ENV": "/evil",
                "MIX_DEPS_PATH": "/ok",
            }
        })
        self.assertEqual(jail_env(repo.root), {"MIX_DEPS_PATH": "/ok"})

    def test_the_dynamic_loader_variables_are_refused(self):
        """They would load code into bubblewrap and the jail's runner, which
        finding those programs first does not prevent."""
        from shepherd_dev.config import jail_env, jail_seed

        names = ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES")
        repo = _Repo({
            "jail_env": {**{name: "/evil" for name in names}, "MIX_DEPS_PATH": "/ok"},
            "jail_seed": {**{name: "/evil" for name in names}, "MIX_BUILD_ROOT": "/warm"},
        })
        self.assertEqual(jail_env(repo.root), {"MIX_DEPS_PATH": "/ok"})
        self.assertEqual(list(jail_seed(repo.root)), ["MIX_BUILD_ROOT"])

    def test_every_python_variable_is_refused(self):
        """Each one changes what an interpreter of ours imports: the jail's
        runner starts on the host before Landlock confines anything."""
        from shepherd_dev.config import jail_env

        names = ("PYTHONPYCACHEPREFIX", "PYTHONUSERBASE", "PYTHONWARNINGS", "PYTHONSAFEPATH")
        repo = _Repo({"jail_env": {**{name: "/evil" for name in names}, "MIX_DEPS_PATH": "/ok"}})
        self.assertEqual(jail_env(repo.root), {"MIX_DEPS_PATH": "/ok"})

    def test_the_glibc_loader_variables_are_refused(self):
        """A gconv module or a locale's converter is code glibc loads into
        bubblewrap and the runner, as LD_PRELOAD is."""
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {"GCONV_PATH": "/evil", "LOCPATH": "/evil", "MIX_DEPS_PATH": "/ok"}})
        self.assertEqual(jail_env(repo.root), {"MIX_DEPS_PATH": "/ok"})

    def test_the_ssh_agent_and_the_docker_config_are_refused(self):
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {
            "SSH_AUTH_SOCK": "/x", "SSH_AGENT_PID": "1", "DOCKER_CONFIG": "/x", "MIX_DEPS_PATH": "/ok",
        }})
        self.assertEqual(jail_env(repo.root), {"MIX_DEPS_PATH": "/ok"})

    def test_shepherd_devs_own_options_are_refused(self):
        """SHEPHERD_DEV_* are read from this process's environment while the
        repo's values sit in it: SHEPHERD_DEV_CODEX_SANDBOX would choose the
        codex worker's sandbox, SHEPHERD_DEV_GATE_ENV_KEEP what the gate keeps."""
        from shepherd_dev.config import jail_env, jail_seed

        repo = _Repo({
            "jail_env": {
                "SHEPHERD_DEV_CODEX_SANDBOX": "danger-full-access", "SHEPHERD_DEV_GATE_ENV_KEEP": "HOME",
                "SHEPHERD_DEV_BACKEND": "auto", "MIX_DEPS_PATH": "/ok",
            },
            "jail_seed": {"SHEPHERD_DEV_RUNS_DIR": str(Path(__file__).parent)},
        })
        self.assertEqual(jail_env(repo.root), {"MIX_DEPS_PATH": "/ok"})
        self.assertEqual(jail_seed(repo.root), {})

    def test_path_is_accepted(self):
        """A repo may point the worker and the gate at its own toolchain."""
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {"PATH": "/opt/toolchain/bin:/usr/bin"}})
        self.assertEqual(jail_env(repo.root), {"PATH": "/opt/toolchain/bin:/usr/bin"})

    def test_each_ignored_key_is_named_with_the_reason(self):
        import contextlib
        import io

        from shepherd_dev.config import jail_env_applied

        repo = _Repo({
            "jail_env": {
                "LD_PRELOAD": "/evil", "PYTHONPATH": "/evil", "DISPLAY": ":9", "GCONV_PATH": "/evil",
                "SSH_AUTH_SOCK": "/x", "SHEPHERD_DEV_CODEX_SANDBOX": "danger-full-access", "MIX_DEPS_PATH": "/ok",
            },
            "jail_seed": {"DYLD_INSERT_LIBRARIES": "/evil"},
        })
        before = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(before)))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), jail_env_applied(repo.root):
            pass
        text = err.getvalue()
        self.assertIn("jail_env sets LD_PRELOAD, which shepherd-dev ignores: it would load code into bubblewrap", text)
        self.assertIn("jail_env sets PYTHONPATH, which shepherd-dev ignores", text)
        self.assertIn("jail_env sets DISPLAY, which shepherd-dev ignores: the jail reads it", text)
        self.assertIn("jail_env sets GCONV_PATH, which shepherd-dev ignores: it would load code", text)
        self.assertIn("jail_env sets SSH_AUTH_SOCK, which shepherd-dev ignores: it would hand the worker", text)
        self.assertIn(
            "jail_env sets SHEPHERD_DEV_CODEX_SANDBOX, which shepherd-dev ignores: it would reconfigure shepherd-dev",
            text,
        )
        self.assertIn("jail_seed sets DYLD_INSERT_LIBRARIES, which shepherd-dev ignores", text)
        self.assertNotIn("MIX_DEPS_PATH", text)

    def test_the_command_servers_socket_variables_are_refused(self):
        """The jail reads these in this process to decide what it hides; a repo
        setting them would choose what the jail leaves in the worker's reach."""
        from shepherd_dev.config import jail_env
        from shepherd_dev.launch import JAIL_SOCKET_ENV

        self.assertIn("TMPDIR", JAIL_SOCKET_ENV)  # macOS: the screen socket deny rule is built from it
        repo = _Repo({"jail_env": {**{name: "/elsewhere" for name in JAIL_SOCKET_ENV}, "MIX_DEPS_PATH": "/ok"}})
        self.assertEqual(jail_env(repo.root), {"MIX_DEPS_PATH": "/ok"})


class JailEnvApplicationTests(unittest.TestCase):
    def setUp(self):
        self._before = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._before)))
        # jail_env_applied with a PATH resolves, and what it keeps would outlive the test
        fresh_programs(self)

    def test_the_values_are_visible_inside_the_block(self):
        from shepherd_dev.config import jail_env_applied

        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "/cache/deps"}})
        self.assertNotIn("MIX_DEPS_PATH", os.environ)
        with jail_env_applied(repo.root):
            self.assertEqual(os.environ["MIX_DEPS_PATH"], "/cache/deps")

    def test_the_environment_is_restored_afterwards(self):
        from shepherd_dev.config import jail_env_applied

        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "/cache/deps"}})
        with jail_env_applied(repo.root):
            pass
        self.assertNotIn("MIX_DEPS_PATH", os.environ)

    def test_a_pre_existing_value_is_restored_not_deleted(self):
        from shepherd_dev.config import jail_env_applied

        os.environ["MIX_DEPS_PATH"] = "/mine"
        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "/cache/deps"}})
        with jail_env_applied(repo.root):
            self.assertEqual(os.environ["MIX_DEPS_PATH"], "/cache/deps")
        self.assertEqual(os.environ["MIX_DEPS_PATH"], "/mine")

    def test_the_environment_is_restored_even_when_the_block_raises(self):
        from shepherd_dev.config import jail_env_applied

        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "/cache/deps"}})
        with self.assertRaises(RuntimeError):
            with jail_env_applied(repo.root):
                raise RuntimeError("boom")
        self.assertNotIn("MIX_DEPS_PATH", os.environ)

    def _tools(self, name):
        """A directory holding a stub of each program the run must find first."""
        from tmpdirs import mkdtemp

        directory = Path(mkdtemp(prefix=f"shepherd-{name}-"))
        for tool in (
            "systemd-run", "env", "sh", "git", "bwrap", "ssh", "claude", "cp", "ps", "notify-send", "osascript",
            "security", "codex", "grok", "node",
        ):
            (directory / tool).write_text("#!/bin/sh\nexit 0\n")
            (directory / tool).chmod(0o755)
        return directory

    def _unresolved(self):
        return fresh_programs(self)

    def test_the_jails_programs_are_found_before_the_repos_path_applies(self):
        """A PATH in jail_env reaches the worker's toolchain; it does not choose
        the systemd-run, env and sh that hold the memory ceiling, the git that
        reads the repo, nor the bubblewrap and ssh of the jail."""
        from unittest import mock

        from shepherd_dev.config import jail_env_applied

        memlimit, programs, exposure = self._unresolved()
        real, planted = self._tools("real"), self._tools("planted")
        os.environ["PATH"] = str(real)
        repo = _Repo({"jail_env": {"PATH": str(planted)}})
        with jail_env_applied(repo.root):
            self.assertEqual(os.environ["PATH"], str(planted))
            argv = memlimit.scope_argv(1 << 30, "/run/user/1")
            self.assertEqual(argv[0], str(real / "env"))
            self.assertEqual(argv[2], str(real / "systemd-run"))
            with mock.patch.object(memlimit, "_active", 1 << 30), \
                    mock.patch.object(memlimit, "runtime_dir", lambda: "/run/user/1"):
                self.assertIn(f"{real / 'sh'} -c true", memlimit.wrap_shell("true"))
            self.assertEqual(programs.argv0("git"), str(real / "git"))
            self.assertEqual(exposure.program("bwrap"), str(real / "bwrap"))
            self.assertEqual(exposure.program("ssh"), str(real / "ssh"))
        self.assertEqual(os.environ["PATH"], str(real))

    def test_a_seed_alone_also_finds_them_first(self):
        from shepherd_dev.config import jail_seed_applied

        _memlimit, programs, exposure = self._unresolved()
        real = self._tools("real")
        os.environ["PATH"] = str(real)
        with jail_seed_applied(_Repo({"jail_seed": {"MIX_BUILD_ROOT": "/absent/warm"}}).root):
            os.environ["PATH"] = "/elsewhere"
            self.assertEqual(programs.which("systemd-run"), str(real / "systemd-run"))
            self.assertEqual(programs.argv0("git"), str(real / "git"))
            self.assertEqual(exposure.program("bwrap"), str(real / "bwrap"))

    def test_a_program_missing_from_the_users_path_stays_missing(self):
        """Not found before the repo's PATH applies means not found: the
        repo's PATH must not supply the memory ceiling's systemd-run or the
        jail's bubblewrap afterwards."""
        from shepherd_dev.config import jail_env_applied

        memlimit, programs, exposure = self._unresolved()
        planted = self._tools("planted")
        os.environ["PATH"] = str(self._tools("empty") / "none")
        with jail_env_applied(_Repo({"jail_env": {"PATH": str(planted)}}).root):
            self.assertIsNone(programs.which("systemd-run"))
            if sys.platform == "linux":
                self.assertIn("not on PATH", memlimit.unavailable_reason(1 << 30) or "")
            self.assertIsNone(exposure.program("bwrap"))

    def test_without_jail_env_nothing_is_resolved_ahead(self):
        """No repo values, no change: each use still looks the program up."""
        from shepherd_dev.config import jail_env_applied, jail_seed_applied

        memlimit, programs, exposure = self._unresolved()
        root = _Repo(None).root
        with jail_seed_applied(root), jail_env_applied(root):
            self.assertEqual(programs._found, {})
            self.assertEqual(exposure._PROGRAMS, {})
        argv = memlimit.scope_argv(1 << 30, "/run/user/1")
        self.assertEqual((argv[0], argv[2]), ("env", "systemd-run"))

    def test_after_resolving_a_program_missing_from_the_users_path_is_refused(self):
        """argv0 does not fall back to the PATH of the moment, which may be the repo's."""
        _memlimit, programs, _exposure = self._unresolved()
        os.environ["PATH"] = str(self._tools("empty") / "none")
        self.assertEqual(programs.argv0("git"), "git")  # not resolved: looked up at exec, as before
        programs.resolve()
        os.environ["PATH"] = str(self._tools("planted"))
        with self.assertRaises(FileNotFoundError):
            programs.argv0("git")
        with self.assertRaises(FileNotFoundError):
            programs.argv0("cp")

    def test_a_kept_environment_without_path_does_not_read_the_live_one(self):
        """shutil.which with no path reads the PATH of the moment; a user who
        started shepherd-dev without one must not get the repo's instead."""
        _memlimit, programs, _exposure = self._unresolved()
        os.environ.pop("PATH", None)
        programs.resolve()
        planted = self._tools("planted")
        os.environ["PATH"] = str(planted)
        for name in ("cp", "ssh", "claude"):
            found = programs.which(name)
            self.assertFalse(found and found.startswith(str(planted)), found)

    def test_a_relative_entry_of_the_users_path_is_not_searched(self):
        """shutil.which reads an empty or "." entry as the current directory, the
        repo's when shepherd-dev runs there, and returns a bare name for an empty
        one, which exec would look up again on the PATH of the moment."""
        _memlimit, programs, _exposure = self._unresolved()
        real, planted = self._tools("real"), self._tools("planted")
        os.environ["PATH"] = f":.:{real}"
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(planted)
        programs.resolve()
        self.assertEqual(programs.which("git"), str(real / "git"))
        self.assertEqual(programs.which("cp"), str(real / "cp"))
        self.assertEqual(programs.argv0("ssh"), str(real / "ssh"))

    def test_the_servers_addresses_are_out_of_the_environment_for_the_command(self):
        """Every launch path inherits this environment, the fallback ones too,
        so none can hand the worker an address the jail does not cover. The
        host side and the gate still have them."""
        from shepherd_dev.launch import worker_environment
        from shepherd_dev.supervisor import gate_env

        _memlimit, programs, exposure = self._unresolved()
        addresses = {
            "DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus", "TMUX": "/tmp/t,1,0",
            "SSH_AUTH_SOCK": "/tmp/agent.sock", "DOCKER_HOST": "unix:///x.sock",
        }
        os.environ.update(addresses)
        with worker_environment():
            for name in addresses:
                self.assertNotIn(name, os.environ)
                self.assertEqual(programs.host_env()[name], addresses[name])
                self.assertEqual(gate_env()[name], addresses[name])
            self.assertEqual(exposure.environ()["TMUX"], "/tmp/t,1,0")  # what the jail hides
        for name, value in addresses.items():
            self.assertEqual(os.environ[name], value)

    def test_what_runs_on_the_host_gets_the_users_environment_not_the_repos(self):
        """The repo's PATH, NODE_OPTIONS, GIT_CONFIG_* and HOME reach the worker
        and the gate. They choose neither the binary nor what it loads for the
        programs shepherd-dev runs on the host for itself."""
        import subprocess
        from types import SimpleNamespace
        from unittest import mock

        from shepherd_dev import (
            cli,
            config,
            history,
            notify,
            preflight,
            remotegate,
            supervisor,
            worker_watchdog,
        )
        from shepherd_dev.config import jail_env_applied
        from shepherd_dev.launch import worker_environment
        from shepherd_dev.providers import codex_exec

        _memlimit, programs, _exposure = self._unresolved()
        real, planted = self._tools("real"), self._tools("planted")
        os.environ["PATH"] = str(real)
        os.environ["HOME"] = str(real)
        os.environ.pop("SHEPHERD_DEV_NO_NOTIFY", None)
        repo = _Repo({"jail_env": {
            "PATH": str(planted), "NODE_OPTIONS": "--require /evil.js", "GIT_CONFIG_COUNT": "1",
            "HOME": str(planted), "SHEPHERD_DEV_CODEX_CMD": str(planted / "codex"),
        }})
        calls = []

        def fake_run(argv, *a, **k):
            calls.append((argv, k.get("env")))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with worker_environment(), jail_env_applied(repo.root):
            self.assertEqual(os.environ["NODE_OPTIONS"], "--require /evil.js")  # the worker's and the gate's
            with mock.patch.object(subprocess, "run", fake_run):
                preflight._default_run_probe(5)
                history._git_sha(repo.root)
                cli._adoption_key(repo.root)
                notify.notify("t", "m")
                worker_watchdog._read_proctable()
                config._clone_dir_fast(repo.root, repo.root / "clone")
                supervisor.fast_copytree(repo.root, Path(str(repo.root) + "-copy"))
                remotegate._remote(remotegate.RemoteGateConfig(ssh="h", repo_dir="/r", test_cmd="true"), "true", 5)
            self.assertEqual(codex_exec.find_codex_bin(), str(real / "codex"))
        self.assertGreaterEqual(len(calls), 7)
        for argv, env in calls:
            with self.subTest(argv=argv[:2]):
                self.assertTrue(str(argv[0]).startswith(str(real)), argv[0])
                self.assertIsNotNone(env)
                self.assertEqual(env["PATH"], str(real))
                self.assertEqual(env["HOME"], str(real))
                self.assertNotIn("NODE_OPTIONS", env)
                self.assertNotIn("GIT_CONFIG_COUNT", env)

    def test_the_gate_detection_and_the_optimizer_run_on_the_users_environment(self):
        """The native gate's `node --version`, its pytest lookup, and the
        optimizer's claude and `git worktree` ran with the PATH of the moment:
        a repo's PATH chose the node shepherd-dev ran on the host, and its
        NODE_OPTIONS was loaded into it, before any gate or person looked."""
        import subprocess
        from types import SimpleNamespace
        from unittest import mock

        from shepherd_dev import config, optimize
        from shepherd_dev.config import jail_env_applied
        from shepherd_dev.launch import worker_environment

        self._unresolved()
        real, planted = self._tools("real"), self._tools("planted")
        (planted / "pytest").write_text("#!/bin/sh\nexit 0\n")
        (planted / "pytest").chmod(0o755)
        os.environ["PATH"] = str(real)
        repo = _Repo({"jail_env": {"PATH": str(planted), "NODE_OPTIONS": "--require /evil.js"}})
        calls = []

        def fake_run(argv, *a, **k):
            if argv[0] == sys.executable:  # the replayed run itself: stop here, the worktree is removed
                raise RuntimeError("not under test")
            calls.append((argv, k.get("env")))
            return SimpleNamespace(returncode=0, stdout="v22.6.0", stderr="")

        case = optimize.ReplayCase(
            feature="f", repo=str(repo.root), sha="abc", test_cmd="true", mode="run", was_success=False
        )
        with worker_environment(), jail_env_applied(repo.root), mock.patch.object(subprocess, "run", fake_run):
            self.assertTrue(config._node_supports_strip_types())
            self.assertFalse(config._pytest_available())  # only the repo's PATH has one
            optimize._propose("m")
            optimize._replay(case, None, 60)
        self.assertEqual(
            [Path(argv[0]).name for argv, _env in calls], ["node", "claude", "git", "git"]
        )
        for argv, env in calls:
            with self.subTest(argv=argv[:2]):
                self.assertTrue(str(argv[0]).startswith(str(real)), argv[0])
                self.assertIsNotNone(env)
                self.assertEqual(env["PATH"], str(real))
                self.assertNotIn("NODE_OPTIONS", env)

    @unittest.skipUnless(sys.platform == "linux", "the Landlock backend is Linux-only")
    def test_the_jails_outer_chain_gets_the_users_environment_and_the_command_the_repos(self):
        """The outer prefix, bubblewrap and the runner run on the host before
        Landlock applies. With the repo's values in their environment, a name
        the refusal list missed reached them; only the confined command gets
        those values, on a memfd the runner reads."""
        import json
        import subprocess
        from unittest import mock

        from vcs_core import _landlock_containment as landlock

        from shepherd_dev.config import jail_env_applied
        from shepherd_dev.launch import worker_environment

        self._unresolved()
        real, planted = self._tools("real"), self._tools("planted")
        os.environ["PATH"] = str(real)
        os.environ.pop("NODE_OPTIONS", None)
        repo = _Repo({"jail_env": {"PATH": str(planted), "NODE_OPTIONS": "--require /evil.js"}})
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"], seen["env"] = argv, kwargs["env"]
            (fd,) = kwargs["pass_fds"]
            seen["command_env"] = json.loads(os.pread(fd, 1 << 20, 0))
            return subprocess.CompletedProcess(argv, 0, "", "")

        backend = landlock.LandlockContainmentBackend()
        with worker_environment(), jail_env_applied(repo.root), \
                mock.patch.object(landlock, "_hiding_argv", lambda keep=(): []), \
                mock.patch.object(landlock.subprocess, "run", fake_run):
            backend.launch(backend.profile_for((str(repo.root),), allow_network=True), repo.root, ["true"])
        self.assertIn(landlock._RUNNER_ENV_FD_SENTINEL, seen["argv"])
        self.assertEqual(seen["env"]["PATH"], str(real))
        self.assertNotIn("NODE_OPTIONS", seen["env"])
        self.assertEqual(seen["command_env"]["PATH"], str(planted))
        self.assertEqual(seen["command_env"]["NODE_OPTIONS"], "--require /evil.js")

    def test_the_planner_gets_the_users_environment_and_claude(self):
        import subprocess
        from types import SimpleNamespace
        from unittest import mock

        from shepherd_dev import planning
        from shepherd_dev.config import jail_env_applied
        from shepherd_dev.launch import worker_environment

        _memlimit, programs, _exposure = self._unresolved()
        real, planted = self._tools("real"), self._tools("planted")
        os.environ["PATH"] = str(real)
        repo = _Repo({"jail_env": {"PATH": str(planted), "NODE_OPTIONS": "--require ./evil.js"}})
        seen = {}

        def fake_run(argv, *a, **k):
            seen["argv0"], seen["env"] = argv[0], k["env"]
            return SimpleNamespace(returncode=1, stdout="", stderr="stub")

        with worker_environment(), jail_env_applied(repo.root), mock.patch.object(subprocess, "run", fake_run):
            planning.plan_targets("f", "a.py", {"a.py"}, repo=repo.root)
        self.assertEqual(seen["argv0"], str(real / "claude"))
        self.assertNotIn("NODE_OPTIONS", seen["env"])
        self.assertEqual(seen["env"]["PATH"], str(real))

    def test_the_preflight_and_the_run_records_read_the_users_home(self):
        """Both read the environment directly, inside the command: a repo's
        CLAUDE_CONFIG_DIR made the preflight judge a token the repo chose, and
        its HOME moved where the run's records are written."""
        import shutil
        import tempfile
        from unittest import mock

        from shepherd_dev import events, preflight
        from shepherd_dev.config import jail_env_applied
        from shepherd_dev.launch import worker_environment

        self._unresolved()
        user_home = Path(tempfile.mkdtemp())
        repo_home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, user_home, True)
        self.addCleanup(shutil.rmtree, repo_home, True)
        for where in (repo_home / "cfg", repo_home / ".claude"):
            where.mkdir(parents=True)
            (where / ".credentials.json").write_text("{}")
        os.environ["HOME"] = str(user_home)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("SHEPHERD_DEV_RUNS_DIR", None)
        repo = _Repo({"jail_env": {"HOME": str(repo_home), "CLAUDE_CONFIG_DIR": str(repo_home / "cfg")}})
        # Off darwin the keychain is not consulted; only the file source is under test.
        with worker_environment(), jail_env_applied(repo.root), mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(os.environ["HOME"], str(repo_home))
            self.assertNotIn("file", preflight.credential_sources())
            self.assertEqual(events._default_runs_root(), user_home / ".shepherd-dev" / "runs")

    def test_the_workers_claude_is_the_one_on_the_users_path(self):
        from types import SimpleNamespace

        from shepherd_dialect.workspace_control import runtime_provider as rp

        from shepherd_dev.config import jail_env_applied
        from shepherd_dev.launch import worker_environment
        from shepherd_dev.supervisor import set_worker_budget

        previous = rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS
        self.addCleanup(setattr, rp, "_WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS", previous)
        _memlimit, _programs, _exposure = self._unresolved()
        real, planted = self._tools("real"), self._tools("planted")
        os.environ["PATH"] = str(real)
        with worker_environment(), jail_env_applied(_Repo({"jail_env": {"PATH": str(planted)}}).root):
            self.assertTrue(set_worker_budget(300))
            invocation = SimpleNamespace(
                provider_id="claude", prompt="p", model_name=None,
                task_lock=SimpleNamespace(task_id="x.y"), kwargs={},
            )
            provider = rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude(invocation)
        self.assertEqual(provider.claude_path, str(real / "claude"))

    def test_an_empty_config_is_a_no_op_block(self):
        from shepherd_dev.config import jail_env_applied

        before = dict(os.environ)
        with jail_env_applied(_Repo(None).root):
            self.assertEqual(dict(os.environ), before)


class CommandsApplyItTests(unittest.TestCase):
    """Wiring, not plumbing: the value has to be in the environment while the
    command runs, because that is the only thing the provider's child
    inherits. A helper nobody calls would pass every test above."""

    def setUp(self):
        self._before = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._before)))
        fresh_programs(self)

    def _seen_by(self, command: str) -> str | None:
        import shepherd_dev.cli as C

        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "/cache/deps"}})
        (repo.root / ".vcscore").mkdir()  # _resolve_repo's workspace marker
        seen = {}

        def _inner(args, repo_root):
            seen["value"] = os.environ.get("MIX_DEPS_PATH")
            return 0

        attr = {"run": "_cmd_run_inner", "run2": "_cmd_run2_inner", "runN": "_cmd_runN_inner"}[command]
        real = getattr(C, attr)
        real_opt = C._maybe_optimize_after
        setattr(C, attr, _inner)
        C._maybe_optimize_after = lambda *a, **k: None
        try:
            args = type("A", (), {"repo": str(repo.root)})()
            {"run": C.cmd_run, "run2": C.cmd_run2, "runN": C.cmd_runN}[command](args)
        finally:
            setattr(C, attr, real)
            C._maybe_optimize_after = real_opt
        return seen.get("value")

    def test_run_applies_it(self):
        self.assertEqual(self._seen_by("run"), "/cache/deps")

    def test_run2_applies_it(self):
        self.assertEqual(self._seen_by("run2"), "/cache/deps")

    def test_runN_applies_it(self):
        # the memory scope and the exposure check run inside, as in run and run2
        self.assertEqual(self._seen_by("runN"), "/cache/deps")

    def test_every_command_takes_the_servers_addresses_out(self):
        import shepherd_dev.cli as C

        os.environ["DISPLAY"] = ":0"
        for command in ("run", "run2", "runN"):
            with self.subTest(command=command):
                repo = _Repo(None)
                (repo.root / ".vcscore").mkdir()
                seen = {}
                attr = {"run": "_cmd_run_inner", "run2": "_cmd_run2_inner", "runN": "_cmd_runN_inner"}[command]
                real, real_opt = getattr(C, attr), C._maybe_optimize_after
                setattr(C, attr, lambda args, root: seen.setdefault("display", os.environ.get("DISPLAY")) and 0)
                C._maybe_optimize_after = lambda *a, **k: None
                try:
                    args = type("A", (), {"repo": str(repo.root)})()
                    {"run": C.cmd_run, "run2": C.cmd_run2, "runN": C.cmd_runN}[command](args)
                finally:
                    setattr(C, attr, real)
                    C._maybe_optimize_after = real_opt
                self.assertIsNone(seen["display"])
                self.assertEqual(os.environ["DISPLAY"], ":0")

    def test_it_does_not_outlive_the_command(self):
        self._seen_by("run")
        self.assertNotIn("MIX_DEPS_PATH", os.environ)


if __name__ == "__main__":
    unittest.main()
