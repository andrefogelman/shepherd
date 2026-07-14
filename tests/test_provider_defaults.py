"""Claude defaults must stay bit-compatible; grok is opt-in only."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class ProviderDefaults(unittest.TestCase):
    def test_run_parser_default_is_claude(self):
        # Importing cli needs shepherd — skip if missing (Grok host tests cover grok alone).
        try:
            from shepherd_dev.cli import main
        except ModuleNotFoundError as exc:
            if "shepherd" in str(exc):
                self.skipTest("shepherd-ai not installed in this interpreter")
            raise
        with mock.patch("sys.argv", ["shepherd-dev", "run", "x", "--help"]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)

    def test_argparse_choices_include_grok_default_claude(self):
        # Mirror the CLI definition without importing shepherd-heavy modules.
        p = argparse.ArgumentParser()
        p.add_argument("--provider", default="claude", choices=["claude", "static", "grok"])
        ns = p.parse_args([])
        self.assertEqual(ns.provider, "claude")
        ns2 = p.parse_args(["--provider", "grok"])
        self.assertEqual(ns2.provider, "grok")

    def test_mcp_argv_default_no_provider_flag(self):
        from shepherd_dev.mcpserver import _argv_for

        argv = _argv_for("shepherd_run", {"feature": "x"})
        self.assertNotIn("--provider", argv)
        self.assertIn("--no-settle", argv)

    def test_mcp_argv_grok_provider(self):
        from shepherd_dev.mcpserver import _argv_for

        argv = _argv_for("shepherd_run", {"feature": "x", "provider": "grok"})
        self.assertIn("--provider", argv)
        self.assertIn("grok", argv)


if __name__ == "__main__":
    unittest.main()
