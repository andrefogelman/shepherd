"""The jail confines writes; the launch policy confines everything else the
worker used to reach: sub-agents, MCP connectors, repo-installed settings,
the browser bridge, and the credential-shaped environment it inherited —
the ssh agent socket in particular, which is how twenty-odd workers with no
local toolchain shipped the repository to a remote host to compile it.

Runnable with: python -m unittest tests.test_launch_policy
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

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


if __name__ == "__main__":
    unittest.main()
