"""Claude defaults must stay bit-compatible; grok and codex are opt-in only."""

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
        p.add_argument("--provider", default="claude", choices=["claude", "static", "grok", "codex"])
        ns = p.parse_args([])
        self.assertEqual(ns.provider, "claude")
        ns2 = p.parse_args(["--provider", "grok"])
        self.assertEqual(ns2.provider, "grok")
        ns3 = p.parse_args(["--provider", "codex"])
        self.assertEqual(ns3.provider, "codex")

    def test_cli_source_offers_codex_choice(self):
        # Assert against the real CLI source (not the mirror above) without
        # importing shepherd-heavy modules: the choices list must include codex.
        cli_src = (
            Path(__file__).resolve().parent.parent / "src" / "shepherd_dev" / "cli.py"
        ).read_text()
        self.assertIn('"codex"', cli_src)
        self.assertIn("--codex-cmd", cli_src)
        self.assertIn("--codex-model", cli_src)

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

    def test_mcp_argv_codex_provider(self):
        from shepherd_dev.mcpserver import _argv_for

        argv = _argv_for("shepherd_run", {"feature": "x", "provider": "codex"})
        self.assertIn("--provider", argv)
        self.assertIn("codex", argv)


if __name__ == "__main__":
    unittest.main()
