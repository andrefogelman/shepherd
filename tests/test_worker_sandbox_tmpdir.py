"""Tests for keeping the jailed worker's Bash sandbox scratch inside the jail.

The worker runs under a syscall jail whose writable roots are the run's clone.
The Claude CLI puts its Bash-sandbox scratch under a tmp root that on darwin is
a hardcoded `/tmp`, ignoring TMPDIR, so the first Bash call tries to mkdir
outside the writable roots, the jail denies it, and every Bash call fails with
`EPERM: operation not permitted, mkdir …`. Nothing surfaces that as a run
failure: the worker just stops running the suite it changed, and the reviewer
reviews by inspection with nothing executed.

The unit tests pin the argv rewrite; the one at the bottom pins it against the
framework's real argv, so a framework that moves its scratch out of the env
prefix turns this red instead of silently restoring the EPERM.
Runnable with: python -m unittest tests.test_worker_sandbox_tmpdir
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.supervisor import (  # noqa: E402
    _swap_perl_killtree,
    _with_sandbox_tmpdir,
)

_providers: Any = None
try:
    from shepherd_dialect import providers as _providers

    _HAS_SUBSTRATE = True
except Exception:  # pragma: no cover - substrate absent
    _HAS_SUBSTRATE = False


def _framework_shaped_argv(tmpdir: str = "/w/.claude-scratch/tmp") -> list[str]:
    """The env-prefixed argv shape the framework builds, trimmed to what matters."""
    return [
        "/usr/bin/perl",
        "-e",
        "alarm shift @ARGV; exec @ARGV or die qq{exec: $!}",
        "900",
        "/usr/bin/env",
        "HOME=/w/.claude-scratch/home",
        "CLAUDE_CONFIG_DIR=/w/.claude-scratch/config",
        f"TMPDIR={tmpdir}",
        "DISABLE_AUTOUPDATER=1",
        "/usr/local/bin/claude",
        "-p",
        "do the thing",
    ]


def _env_value(argv, name: str) -> str | None:
    for item in argv:
        text = str(item)
        if text.startswith(f"{name}="):
            return text[len(name) + 1 :]
    return None


class SandboxTmpdirRewrite(unittest.TestCase):
    def test_the_sandbox_root_is_pointed_at_the_jailed_scratch(self):
        argv = _with_sandbox_tmpdir(_framework_shaped_argv())
        self.assertEqual(
            _env_value(argv, "CLAUDE_CODE_TMPDIR"), "/w/.claude-scratch/tmp"
        )

    def test_the_sandbox_root_tracks_tmpdir_rather_than_a_second_guess(self):
        # Read off the argv, so a framework that moves its scratch moves this too.
        argv = _with_sandbox_tmpdir(_framework_shaped_argv("/elsewhere/scratch/tmp"))
        self.assertEqual(
            _env_value(argv, "CLAUDE_CODE_TMPDIR"),
            _env_value(argv, "TMPDIR"),
        )

    def test_it_sits_in_the_env_prefix_and_not_past_the_cli(self):
        # `/usr/bin/env` stops consuming assignments at the first non-assignment:
        # inserted after the CLI it would become an argument to claude instead.
        argv = _with_sandbox_tmpdir(_framework_shaped_argv())
        self.assertLess(
            argv.index("CLAUDE_CODE_TMPDIR=/w/.claude-scratch/tmp"),
            argv.index("/usr/local/bin/claude"),
        )

    def test_nothing_else_in_the_launch_moves(self):
        base = _framework_shaped_argv()
        argv = _with_sandbox_tmpdir(base)
        self.assertEqual([item for item in argv if not item.startswith("CLAUDE_CODE_TMPDIR=")], base)
        self.assertEqual(len(argv), len(base) + 1)

    def test_an_existing_setting_is_left_alone(self):
        base = _framework_shaped_argv()
        base.insert(8, "CLAUDE_CODE_TMPDIR=/already/chosen")
        self.assertEqual(_with_sandbox_tmpdir(base), base)

    def test_an_env_prefix_without_tmpdir_is_left_alone(self):
        base = [item for item in _framework_shaped_argv() if not item.startswith("TMPDIR=")]
        self.assertEqual(_with_sandbox_tmpdir(base), base)

    def test_the_input_argv_is_not_mutated(self):
        base = _framework_shaped_argv()
        before = list(base)
        _with_sandbox_tmpdir(base)
        self.assertEqual(base, before)

    def test_the_killtree_swap_composes_with_it(self):
        # Both rewrites run on every launch; neither may undo the other.
        argv = _swap_perl_killtree(_with_sandbox_tmpdir(_framework_shaped_argv()))
        self.assertEqual(
            _env_value(argv, "CLAUDE_CODE_TMPDIR"), "/w/.claude-scratch/tmp"
        )
        self.assertIn("kill('KILL', -$pid)", argv[2])


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class AgainstTheFrameworkArgv(unittest.TestCase):
    """The rewrite is only worth anything against the argv actually launched."""

    def _argv(self, working_path: str = "/w"):
        provider = _providers.ClaudeHeadlessProvider(
            provider_id="test", prompt="do the thing", model=None, budget_seconds=900
        )
        return list(provider.command_argv(working_path, "/usr/local/bin/claude"))

    def test_the_framework_still_carries_tmpdir_in_the_env_prefix(self):
        # The hook this fix hangs on. If it ever goes, the rewrite degrades to a
        # no-op and the EPERM comes back silently — so fail here, loudly.
        self.assertIsNotNone(
            _env_value(self._argv(), "TMPDIR"),
            "framework argv no longer sets TMPDIR: _with_sandbox_tmpdir is now a no-op",
        )

    def test_the_sandbox_root_lands_inside_the_working_path(self):
        argv = _with_sandbox_tmpdir(self._argv("/w"))
        root = _env_value(argv, "CLAUDE_CODE_TMPDIR")
        self.assertIsNotNone(root)
        assert root is not None  # narrowing, for type checkers
        # Inside the jail's writable root is the whole point: outside it, the
        # mkdir is denied and every Bash call in the worker fails.
        self.assertTrue(Path(root).is_relative_to(Path("/w")), root)
        self.assertNotEqual(Path(root), Path("/w"))


if __name__ == "__main__":
    unittest.main()
