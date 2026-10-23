"""The jail confines writes; the launch policy confines everything else the
worker used to reach: sub-agents, MCP connectors, repo-installed settings,
the browser bridge, and the credential-shaped environment it inherited —
the ssh agent socket in particular, which is how twenty-odd workers with no
local toolchain shipped the repository to a remote host to compile it.

Runnable with: python -m unittest tests.test_launch_policy
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hostprograms import fresh_programs  # noqa: E402

from shepherd_dev import cli, launch  # noqa: E402
from shepherd_dev.launch import (  # noqa: E402
    DEFAULT_DISALLOWED_TOOLS,
    LaunchPolicy,
    describe,
    harden_argv,
)


def _claude_argv() -> list[str]:
    """The env-prefixed shape the framework builds, trimmed to what matters."""
    return [
        "/usr/bin/perl", "-e", "alarm shift @ARGV; exec @ARGV or die qq{exec: $!}", "900",
        "/usr/bin/env",
        "HOME=/w/.claude-scratch/home",
        "CLAUDE_CONFIG_DIR=/w/.claude-scratch/config",
        "TMPDIR=/w/.claude-scratch/tmp",
        "DISABLE_AUTOUPDATER=1",
        "/usr/local/bin/claude", "-p", "do the thing",
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions", "--tools", "default",
    ]


def _flag_value(argv: list[str], flag: str) -> str | None:
    for i, item in enumerate(argv):
        if item == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


class ToolsAndIntegrations(unittest.TestCase):
    def test_the_denied_tools_and_the_flags_are_appended(self):
        argv = harden_argv(_claude_argv(), LaunchPolicy(), environ={})
        denied = _flag_value(argv, "--disallowedTools")
        self.assertIsNotNone(denied)
        for name in ("Agent", "TaskCreate", "TaskUpdate", "ToolSearch", "WebFetch", "WebSearch"):
            self.assertIn(name, denied.split(","))
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-chrome", argv)
        self.assertEqual(_flag_value(argv, "--setting-sources"), "user")

    def test_the_framework_argv_is_kept_intact_ahead_of_the_additions(self):
        base = _claude_argv()
        argv = harden_argv(base, LaunchPolicy(), environ={})
        self.assertEqual(argv[: len(base)], base)

    def test_bash_read_edit_write_are_never_denied(self):
        for name in ("Bash", "Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"):
            self.assertNotIn(name, DEFAULT_DISALLOWED_TOOLS)

    def test_hardening_twice_adds_nothing_twice(self):
        once = harden_argv(_claude_argv(), LaunchPolicy(), environ={})
        twice = harden_argv(once, LaunchPolicy(), environ={})
        self.assertEqual(once, twice)

    def test_a_non_claude_argv_is_untouched(self):
        argv = ["/bin/sh", "-c", "echo hi"]
        self.assertEqual(harden_argv(list(argv), LaunchPolicy(), environ={"GH_TOKEN": "x"}), argv)

    def test_a_disabled_policy_changes_nothing(self):
        base = _claude_argv()
        self.assertEqual(
            harden_argv(base, LaunchPolicy(enabled=False), environ={"GH_TOKEN": "x"}), base
        )

    def test_a_disabled_policy_still_unsets_the_command_servers_addresses(self):
        """`worker.harden: false` lives in the repo under review: it turns the
        hardening off, never the addresses the jail does not cover."""
        base = _claude_argv()
        env = {
            "GH_TOKEN": "x", "TMUX": "/tmp/tmux-1/default,1,0", "DOCKER_HOST": "tcp://h:2375",
            "SSH_AUTH_SOCK": "/tmp/ssh-x/agent.1",
        }
        argv = harden_argv(base, LaunchPolicy(enabled=False), environ=env)
        at = argv.index("/usr/bin/env") + 1
        self.assertEqual(argv[at:at + 6], ["-u", "DOCKER_HOST", "-u", "SSH_AUTH_SOCK", "-u", "TMUX"])
        self.assertNotIn("GH_TOKEN", argv)
        self.assertNotIn("--strict-mcp-config", argv)
        policy = LaunchPolicy.from_config(None, {"worker": {"harden": False}})
        self.assertEqual(describe(policy, env)["env_scrubbed"], ["DOCKER_HOST", "SSH_AUTH_SOCK", "TMUX"])


class EnvironmentScrub(unittest.TestCase):
    def test_present_credentials_are_unset_right_after_env(self):
        env = {"SSH_AUTH_SOCK": "/tmp/agent", "GH_TOKEN": "ghp", "PATH": "/usr/bin", "HOME": "/me"}
        argv = harden_argv(_claude_argv(), LaunchPolicy(), environ=env)
        i = argv.index("/usr/bin/env")
        self.assertEqual(argv[i + 1:i + 5], ["-u", "GH_TOKEN", "-u", "SSH_AUTH_SOCK"])
        # the substrate's own assignments follow the unsets, so they still apply
        self.assertTrue(argv[i + 5].startswith("HOME="))

    def test_absent_names_produce_no_unset(self):
        argv = harden_argv(_claude_argv(), LaunchPolicy(), environ={"PATH": "/usr/bin"})
        self.assertNotIn("-u", argv)

    def test_secret_shaped_names_match_by_pattern(self):
        env = {
            "MYCORP_API_KEY": "1", "DEPLOY_TOKEN": "2", "DB_PASSWORD": "3",
            "SOME_SECRET": "4", "A_PRIVATE_KEY": "5", "GCP_CREDENTIALS": "6",
            "TOKEN_COUNT": "7",  # a prefix, not a suffix: kept
        }
        names = LaunchPolicy().scrub_names(env)
        self.assertEqual(
            names,
            ["A_PRIVATE_KEY", "DB_PASSWORD", "DEPLOY_TOKEN", "GCP_CREDENTIALS", "MYCORP_API_KEY", "SOME_SECRET"],
        )

    def test_the_api_credentials_are_never_scrubbed(self):
        env = {
            "ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t",
            "CLAUDE_CODE_OAUTH_TOKEN": "o", "ANTHROPIC_BASE_URL": "u",
        }
        self.assertEqual(LaunchPolicy().scrub_names(env), [])

    def test_jail_env_and_env_keep_survive(self):
        config = {
            "jail_env": {"MIX_DEPS_PATH": "~/cache", "HEX_API_KEY": "h"},
            "worker": {"env_keep": ["NPM_TOKEN"], "env_scrub": ["EXTRA_THING"]},
        }
        policy = LaunchPolicy.from_config(None, config)
        env = {"HEX_API_KEY": "h", "NPM_TOKEN": "n", "EXTRA_THING": "e", "GH_TOKEN": "g"}
        self.assertEqual(policy.scrub_names(env), ["EXTRA_THING", "GH_TOKEN"])

    def test_values_never_appear_anywhere(self):
        env = {"GH_TOKEN": "ghp_SECRETVALUE"}
        argv = harden_argv(_claude_argv(), LaunchPolicy(), environ=env)
        self.assertNotIn("ghp_SECRETVALUE", " ".join(argv))
        self.assertNotIn("ghp_SECRETVALUE", str(describe(LaunchPolicy(), env)))
        self.assertEqual(describe(LaunchPolicy(), env)["env_scrubbed"], ["GH_TOKEN"])


class FromConfig(unittest.TestCase):
    def test_defaults_without_a_config(self):
        policy = LaunchPolicy.from_config(None, {})
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.disallowed_tools, DEFAULT_DISALLOWED_TOOLS)

    def test_the_repo_may_replace_the_tool_list_and_turn_hardening_off(self):
        policy = LaunchPolicy.from_config(None, {"worker": {"disallowed_tools": ["Agent"], "harden": False}})
        self.assertEqual(policy.disallowed_tools, ("Agent",))
        self.assertFalse(policy.enabled)

    def test_it_reads_the_repo_config_file(self):
        from shepherd_dev import config

        repo = Path(tempfile.mkdtemp(prefix="shepherd-launch-"))
        config.save_config(repo, {"worker": {"env_keep": ["KEEP_ME_TOKEN"]}, "jail_env": {"MIX_HOME": "/x"}})
        policy = LaunchPolicy.from_config(repo)
        self.assertIn("KEEP_ME_TOKEN", policy.env_keep)
        self.assertIn("MIX_HOME", policy.env_keep)


try:
    from shepherd_dialect.workspace_control import runtime_provider as _rp

    _HAS_SUBSTRATE = True
except Exception:  # pragma: no cover - substrate absent
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class ThroughTheTransport(unittest.TestCase):
    """set_worker_budget's provider must emit the hardened argv against the
    framework's REAL command_argv, with the killtree swap still in place."""

    def setUp(self):
        self._previous = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS

    def tearDown(self):
        _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = self._previous

    def test_the_real_argv_is_hardened(self):
        import os
        from unittest.mock import patch

        from shepherd_dev.supervisor import _KILLTREE_PERL, set_worker_budget

        policy = LaunchPolicy.from_config(None, {})
        self.assertTrue(set_worker_budget(300, launch=policy))
        invocation = SimpleNamespace(
            provider_id="claude", prompt="p", model_name=None,
            task_lock=SimpleNamespace(task_id="x.y"), kwargs={},
        )
        provider = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude(invocation)
        with patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/agent.sock"}):
            argv = provider.command_argv("/w", "/usr/local/bin/claude")
        self.assertEqual(argv[2], _KILLTREE_PERL)
        i = next(k for k, a in enumerate(argv) if str(a).endswith("/env"))
        # The unsets sit between the env binary and its first assignment. The
        # developer's own environment may add names (any *_API_KEY it carries),
        # so membership is asserted, not position.
        unset = []
        j = i + 1
        while j + 1 < len(argv) and str(argv[j]) == "-u":
            unset.append(str(argv[j + 1]))
            j += 2
        self.assertIn("SSH_AUTH_SOCK", unset)
        self.assertTrue(str(argv[j]).startswith("HOME="), argv[j])
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-chrome", argv)
        self.assertIsNotNone(_flag_value([str(a) for a in argv], "--disallowedTools"))
        self.assertEqual(_flag_value([str(a) for a in argv], "CLAUDE_CODE_TMPDIR"), None)  # env assignment, not a flag
        self.assertTrue(any(str(a).startswith("CLAUDE_CODE_TMPDIR=") for a in argv))


class _Jail:
    """Stands in for shepherd-ai's Landlock module (0.3.7 or later)."""

    class JailExposureWarning(RuntimeWarning):
        pass

    def __init__(self, message=None, available=(True, "stub")):
        self._message = message
        self._available = available

    def exposure(self):
        return self._message

    def LandlockContainmentBackend(self):  # noqa: N802 - mirrors the module's class
        return SimpleNamespace(available=lambda: self._available)


class JailExposure(unittest.TestCase):
    """Where the jail leaves a command server reachable, the user is told once
    and the run goes ahead; elsewhere nothing is printed."""

    def setUp(self):
        fresh_programs(self)

    def _exposure(self, platform, backend):
        with mock.patch.object(launch.sys, "platform", platform), \
                mock.patch.object(launch, "landlock_backend", lambda: backend):
            return launch.jail_exposure()

    def test_neither_linux_nor_macos_reports_nothing(self):
        self.assertIsNone(self._exposure("win32", None))

    def _seatbelt(self, backend_cls):
        try:
            import vcs_core
        except Exception:  # pragma: no cover - substrate absent
            self.skipTest("shepherd substrate not installed")
        stand_in = types.ModuleType("vcs_core._seatbelt_containment")
        stand_in.SeatbeltContainmentBackend = backend_cls
        with mock.patch.dict(sys.modules, {"vcs_core._seatbelt_containment": stand_in}), \
                mock.patch.object(vcs_core, "_seatbelt_containment", stand_in, create=True), \
                mock.patch.object(launch.sys, "platform", "darwin"):
            return launch.jail_exposure()

    def test_macos_relays_the_sandbox_message(self):
        class Backend:
            def available(self):
                return (True, "stub")

            def exposure(self):
                return "/var/run/docker.sock is writable by this user"

        self.assertEqual(self._seatbelt(Backend), "/var/run/docker.sock is writable by this user")

    def test_macos_reports_an_old_sandbox(self):
        # before 0.3.8 the profile lets a confined command connect to tmux's socket,
        # and before 0.3.14 it reads what to deny from the live environment
        class Backend:
            def available(self):
                return (True, "stub")

        self.assertIn("0.3.14", self._seatbelt(Backend) or "")

    def test_a_backend_that_fails_its_check_is_reported_and_the_run_goes_on(self):
        # the contract: a check that raises is a warning, never a stopped run
        class Backend:
            def available(self):
                return (True, "stub")

            def exposure(self):
                raise OSError("permission denied reading /run/user/1000")

        self.assertIn("could not check", self._seatbelt(Backend) or "")
        jail = _Jail(None)
        jail.exposure = mock.Mock(side_effect=OSError("permission denied reading /run/user/1000"))
        message = self._exposure("linux", jail) or ""
        self.assertIn("could not check what the jail leaves in reach", message)
        self.assertIn("running anyway", message)
        # the CLI silences the repeat of what it printed for the rest of the
        # process; kept to this test so later tests see the default filters
        with warnings.catch_warnings():
            printed = self._warn("claude", jail)
        self.assertIn("permission denied", printed)

    def test_macos_without_the_substrate_reports_nothing(self):
        with mock.patch.dict(sys.modules, {"vcs_core._seatbelt_containment": None}), \
                mock.patch.object(launch.sys, "platform", "darwin"):
            self.assertIsNone(launch.jail_exposure())

    def test_macos_without_a_working_sandbox_reports_nothing(self):
        class Backend:
            def available(self):
                return (False, "/usr/bin/sandbox-exec not found")

            def exposure(self):
                raise AssertionError("not asked when there is no sandbox")

        self.assertIsNone(self._seatbelt(Backend))

    def test_a_linux_jail_check_that_raises_is_reported(self):
        jail = _Jail("not read")
        jail.LandlockContainmentBackend = lambda: SimpleNamespace(
            available=mock.Mock(side_effect=OSError("landlock_create_ruleset: EPERM"))
        )
        message = self._exposure("linux", jail) or ""
        self.assertIn("could not check what the jail leaves in reach", message)
        self.assertIn("EPERM", message)

    def test_linux_relays_the_backend_message(self):
        message = "the jail does not hide X. Install bubblewrap"
        self.assertEqual(self._exposure("linux", _Jail(message)), message)

    def test_an_old_backend_is_reported_not_passed_over(self):
        # 0.3.5 and older hide nothing and say nothing; up to 0.3.13 the jail
        # reads what to hide from an environment the command has emptied
        self.assertIn("0.3.14", self._exposure("linux", None) or "")

    def test_a_host_without_the_jail_reports_nothing(self):
        jail = _Jail("hidden or not, there is no jail", available=(False, "ENOSYS"))
        self.assertIsNone(self._exposure("linux", jail))

    def test_backend_needs_every_0_3_7_name(self):
        try:
            import vcs_core
        except Exception:  # pragma: no cover - substrate absent
            self.skipTest("shepherd substrate not installed")
        partial = types.ModuleType("vcs_core._landlock_containment")
        partial.set_outer_prefix = lambda argv: None
        partial.service_manager_exposure = lambda: None  # 0.3.6

        def installed():
            # `from vcs_core import _landlock_containment` reads the package
            # attribute first, so both places get the stand-in
            return mock.patch.dict(sys.modules, {"vcs_core._landlock_containment": partial}), \
                mock.patch.object(vcs_core, "_landlock_containment", partial, create=True)

        modules, attribute = installed()
        with modules, attribute:
            self.assertIsNone(launch.landlock_backend())
        partial.exposure = lambda: None
        partial.JailExposureWarning = RuntimeWarning
        modules, attribute = installed()
        with modules, attribute:
            self.assertIs(launch.landlock_backend(), partial)

    def test_a_jail_that_reads_the_live_environment_is_old(self):
        """Before 0.3.14 the jail read what to hide from os.environ, which
        worker_environment empties of the servers' addresses for the command:
        that jail would hide nothing, so it counts as not hiding."""
        try:
            from vcs_core import _jail_exposure
        except Exception:  # pragma: no cover - substrate absent
            self.skipTest("shepherd substrate not installed")
        old = types.ModuleType("vcs_core._jail_exposure")
        old.resolve_programs = _jail_exposure.resolve_programs  # 0.3.13: no environ
        with mock.patch.dict(sys.modules, {"vcs_core._jail_exposure": old}), \
                mock.patch("vcs_core._jail_exposure", old, create=True):
            self.assertIsNone(launch.landlock_backend())
            self.assertIn("0.3.14", self._exposure("linux", None) or "")

            class Backend:
                def available(self):
                    return (True, "stub")

                def exposure(self):
                    return None

            self.assertIn("0.3.14", self._seatbelt(Backend) or "")

    def _warn(self, provider, jail, platform="linux"):
        err = io.StringIO()
        with mock.patch.object(launch, "landlock_backend", lambda: jail), \
                mock.patch.object(launch.sys, "platform", platform), \
                mock.patch.object(cli.sys, "platform", platform), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(cli._warn_jail_exposure(SimpleNamespace(provider=provider)))
        return err.getvalue()

    def test_cli_warns_once_and_silences_only_the_repeat_of_that_text(self):
        jail = _Jail("the jail does not hide X. Install bubblewrap")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            printed = self._warn("claude", jail)
            # the backend's per-launch repeat of the printed text is dropped
            warnings.warn("the jail does not hide X. Install bubblewrap", jail.JailExposureWarning, stacklevel=1)
            # a different finding under the same category still reaches the user
            warnings.warn("a socket was still reachable inside the jail", jail.JailExposureWarning, stacklevel=1)
            warnings.warn("an unrelated runtime warning", RuntimeWarning, stacklevel=1)
        self.assertEqual(printed, "warning: the jail does not hide X. Install bubblewrap\n")
        self.assertEqual(
            [str(w.message) for w in caught],
            ["a socket was still reachable inside the jail", "an unrelated runtime warning"],
        )

    def test_a_longer_warning_that_starts_with_the_printed_text_still_shows(self):
        """warnings.filterwarnings matches from the start of the message: the
        printed text plus a new exposure found later must not be silenced."""
        jail = _Jail("the jail does not hide X. Install bubblewrap")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self._warn("claude", jail)
            longer = "the jail does not hide X. Install bubblewrap Also: /run/docker.sock is writable"
            warnings.warn(longer, jail.JailExposureWarning, stacklevel=1)
        self.assertEqual([str(w.message) for w in caught], [longer])

    def test_cli_silent_when_nothing_is_open(self):
        self.assertEqual(self._warn("claude", _Jail(None)), "")
        self.assertEqual(self._warn("static", _Jail("open")), "")

    def test_other_providers_are_told_the_mask_does_not_cover_them(self):
        for provider in ("codex", "grok"):
            with self.subTest(provider=provider):
                printed = self._warn(provider, _Jail(None))
                self.assertIn(f"--provider {provider} runs under its own CLI's sandbox", printed)
                self.assertIn("bubblewrap mask", printed)
                on_macos = self._warn(provider, _Jail(None), platform="darwin")
                self.assertIn(f"--provider {provider} runs under its own CLI's sandbox", on_macos)
                self.assertIn("Seatbelt rules", on_macos)
                self.assertEqual(self._warn(provider, _Jail(None), platform="win32"), "")

    def test_every_run_command_warns_before_it_starts(self):
        calls = []
        args = SimpleNamespace(provider="claude", repo=None)
        with mock.patch.object(cli, "_apply_run_limits", lambda *a, **k: None), \
                mock.patch.object(cli, "_apply_memory_limit", lambda *a, **k: True), \
                mock.patch.object(cli, "_warn_jail_exposure", lambda a: calls.append(a)), \
                mock.patch.object(cli, "_auth_preflight", lambda *a, **k: False), \
                mock.patch.object(cli, "_resolve_repo", lambda repo: Path(".")), \
                mock.patch.object(cli.config, "jail_seed_applied", lambda root: contextlib.nullcontext()), \
                mock.patch.object(cli.config, "jail_env_applied", lambda root: contextlib.nullcontext()):
            for name, run in (
                ("run", lambda: cli._cmd_run_inner(args, Path("."))),
                ("run2", lambda: cli._cmd_run2_inner(args, Path("."))),
                ("runN", lambda: cli.cmd_runN(args)),
            ):
                with self.subTest(command=name):
                    calls.clear()
                    self.assertEqual(run(), 2)  # stopped at the auth preflight, after the warning
                    self.assertEqual(calls, [args])


class CommandServerAddresses(unittest.TestCase):
    """The addresses of servers that run commands for the user are unset for
    the worker: the jail hides the sockets it finds, not the ones these name."""

    def test_unset(self):
        names = ("TMUX", "STY", "DBUS_SESSION_BUS_ADDRESS", "DOCKER_HOST", "DISPLAY", "XAUTHORITY")
        env = {name: "x" for name in names}
        env["PATH"] = "/usr/bin"
        self.assertEqual(sorted(describe(LaunchPolicy(), env)["env_scrubbed"]), sorted(names))


class SocketVariablesAreNeverKept(unittest.TestCase):
    """`env_keep` and `jail_env` cannot hand the worker a command server's address."""

    def test_neither_env_keep_nor_jail_env_keeps_them(self):
        policy = LaunchPolicy.from_config(
            None,
            {"worker": {"env_keep": ["TMUX", "MY_TOOL_TOKEN"]}, "jail_env": {"DOCKER_HOST": "tcp://x:2375"}},
        )
        self.assertFalse(policy.keeps("TMUX"))
        self.assertFalse(policy.keeps("DOCKER_HOST"))
        self.assertFalse(policy.keeps("DISPLAY"))
        self.assertTrue(policy.keeps("MY_TOOL_TOKEN"))
        environ = {"TMUX": "/tmp/tmux-1/default,1,0", "DOCKER_HOST": "tcp://x:2375", "MY_TOOL_TOKEN": "t"}
        self.assertEqual(set(policy.scrub_names(environ)), {"TMUX", "DOCKER_HOST"})


class PlainLaunchFallback(unittest.TestCase):
    """When shepherd-dev's provider cannot be built the launch still goes, and
    the user is told once what it goes without."""

    def test_warned_once_naming_what_is_lost(self):
        from shepherd_dev import supervisor

        self.addCleanup(setattr, supervisor, "_plain_launch_warned", supervisor._plain_launch_warned)
        supervisor._plain_launch_warned = False
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            supervisor._warn_plain_launch(TypeError("unexpected keyword 'budget_seconds'"))
            supervisor._warn_plain_launch(TypeError("again"))
        printed = err.getvalue()
        self.assertEqual(printed.count("warning:"), 1)
        self.assertIn("unexpected keyword 'budget_seconds'", printed)
        for lost in ("tool denylist", "env scrub", "TMPDIR", "process-tree kill"):
            self.assertIn(lost, printed)


if __name__ == "__main__":
    unittest.main()
