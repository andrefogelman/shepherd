"""The menu's CLI integration: the isatty gate and the equivalent-command
line. Runnable with: python -m unittest tests.test_menu_cli
"""

from __future__ import annotations

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


class EquivalentCommandTests(unittest.TestCase):
    def test_the_printed_command_is_the_argv_that_runs(self):
        from shepherd_dev.cli import _equivalent_command

        line = _equivalent_command(["run", "add CPF validation", "--review-panel", "3"])
        self.assertIn("shepherd-dev run", line)
        self.assertIn("'add CPF validation'", line)  # shlex quotes the space
        self.assertIn("--review-panel 3", line)


if __name__ == "__main__":
    unittest.main()
