"""The menu's CLI integration: the isatty gate and the equivalent-command
line. Runnable with: python -m unittest tests.test_menu_cli
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class IsattyGateTests(unittest.TestCase):
    def test_piped_stdin_gets_todays_usage_error_not_a_menu(self):
        """A CI job running a bare `shepherd-dev` today gets exit 2 and a
        usage error. Adding the menu must not change that."""
        result = subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli"],
            input="", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertNotIn("supervised AI development\n\n  develop", result.stdout)

    def test_bare_invocation_with_closed_stdin_gets_a_usage_error_not_a_crash(self):
        """I1 regression: when fd 0 is closed outright (not merely piped),
        CPython sets sys.stdin = None at startup, so a bare `.isatty()`
        call raises AttributeError instead of degrading to today's usage
        error. `stdin=subprocess.DEVNULL` doesn't reproduce this — DEVNULL
        is a valid, non-tty stdin, not a closed one — so fd 0 is closed
        directly in a child before exec'ing into the CLI."""
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import os, sys; os.close(0); "
                "os.execv(sys.executable, [sys.executable, '-m', 'shepherd_dev.cli'])",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_the_menu_is_skipped_when_arguments_are_present(self):
        """Any argv beyond the program name takes the ordinary path."""
        result = subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli", "--help"],
            input="", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)

    def test_the_menu_is_skipped_when_a_real_command_has_arguments(self):
        """A subprocess run of a real command with arguments must behave
        exactly as before: no menu, no equivalent-command banner."""
        result = subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli", "status", "--json"],
            input="", capture_output=True, text=True,
        )
        self.assertNotIn("equivalent:", result.stdout)
        self.assertNotIn("equivalent:", result.stderr)


class _TTYStdin(io.StringIO):
    """A stdin stand-in that reports itself as a tty without needing a real
    terminal — the isatty gate is what routes a bare invocation to the
    menu, so the in-process tests below have to satisfy it."""

    def isatty(self) -> bool:
        return True


class BareInvocationOpensMenuTests(unittest.TestCase):
    """I5: main()'s new branch driven in-process. `tests/test_menu_cli.py`
    otherwise only pins the negative side (piped stdin, args present) —
    nothing asserted that an interactive bare invocation actually opens the
    menu, that quitting returns 0 without parsing, or that an accepted argv
    is re-parsed and executed. Patches sys.argv, sys.stdin (to satisfy the
    isatty gate) and shepherd_dev.menu.run_menu, then calls the real
    cli.main() — no subprocess and no pty needed."""

    def test_quitting_the_menu_returns_zero_and_parses_nothing(self):
        from unittest.mock import patch

        from shepherd_dev.cli import main

        with patch("sys.argv", ["shepherd-dev"]), \
             patch("sys.stdin", _TTYStdin("")), \
             patch("shepherd_dev.menu.run_menu", return_value=None) as run_menu:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main()
        self.assertEqual(rc, 0)
        run_menu.assert_called_once()
        self.assertNotIn("equivalent:", buf.getvalue())

    def test_an_accepted_argv_is_reparsed_and_the_command_actually_runs(self):
        from unittest.mock import patch

        from shepherd_dev.cli import main

        def fake_run_menu(argv_out):
            argv_out.extend(["status", "--json"])
            return 0

        with patch("sys.argv", ["shepherd-dev"]), \
             patch("sys.stdin", _TTYStdin("")), \
             patch("shepherd_dev.menu.run_menu", side_effect=fake_run_menu):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main()
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("equivalent:\n  shepherd-dev status --json", out)
        # the command actually ran (not just printed): cmd_status --json's
        # own output — the JSON array it prints — follows the banner.
        after_banner = out.rsplit("─" * 40, 1)[-1]
        json.loads(after_banner)

    def test_a_rejected_argv_exits_with_the_parsers_usage_error(self):
        from unittest.mock import patch

        from shepherd_dev.cli import main

        def fake_run_menu(argv_out):
            argv_out.extend(["run"])  # missing the required `feature` positional
            return 0

        with patch("sys.argv", ["shepherd-dev"]), \
             patch("sys.stdin", _TTYStdin("")), \
             patch("shepherd_dev.menu.run_menu", side_effect=fake_run_menu):
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as cm:
                    main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("equivalent:", out_buf.getvalue())
        self.assertIn("the following arguments are required: feature", err_buf.getvalue())


class EquivalentCommandTests(unittest.TestCase):
    def test_the_printed_command_is_the_argv_that_runs(self):
        from shepherd_dev.cli import _equivalent_command

        line = _equivalent_command(["run", "add CPF validation", "--review-panel", "3"])
        self.assertIn("shepherd-dev run", line)
        self.assertIn("'add CPF validation'", line)  # shlex quotes the space
        self.assertIn("--review-panel 3", line)


if __name__ == "__main__":
    unittest.main()
