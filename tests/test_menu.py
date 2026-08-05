"""Tests for the interactive launch menu. Runnable with:
python -m unittest tests.test_menu
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _parser_options(command: str) -> dict[str, argparse.Action]:
    """Every real option of one subcommand, keyed the way the table keys it.

    Uses argparse's private _actions deliberately and ONLY here: production
    code reads the table, so if argparse changes its internals a test fails
    loudly instead of shipping a broken menu.
    """
    from shepherd_dev.cli import build_parser

    subs = [a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)]
    out: dict[str, argparse.Action] = {}
    for action in subs[0].choices[command]._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        out[action.option_strings[-1] if action.option_strings else action.dest] = action
    return out


class OptionTableDriftTests(unittest.TestCase):
    """The table duplicates what the parser knows. These tests are what make
    that duplication safe: a flag added, or a flag whose shape changes, fails
    here until the table is updated. Same guard as RendererDriftTests."""

    def test_mcp_is_never_listed(self):
        from shepherd_dev.menu import COMMANDS, OPTIONS

        self.assertNotIn("mcp", COMMANDS)
        self.assertNotIn("mcp", OPTIONS)

    def test_every_exposed_subcommand_has_a_table(self):
        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import COMMANDS, OPTIONS

        subs = [a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)]
        real = set(subs[0].choices) - {"mcp"}
        self.assertEqual(set(COMMANDS), real)
        self.assertEqual(set(OPTIONS), real)

    def test_every_flag_is_classified(self):
        from shepherd_dev.menu import ADVANCED, MAIN, COMMANDS, OPTIONS

        for command in COMMANDS:
            with self.subTest(command=command):
                table = {o.flag or o.dest: o for o in OPTIONS[command]}
                self.assertEqual(
                    set(table),
                    set(_parser_options(command)),
                    f"{command}: every flag must be classified main or advanced",
                )
                for opt in OPTIONS[command]:
                    self.assertIn(opt.tier, (MAIN, ADVANCED))

    def test_each_kind_matches_the_parser(self):
        from shepherd_dev.menu import COMMANDS, OPTIONS

        for command in COMMANDS:
            actual = _parser_options(command)
            for opt in OPTIONS[command]:
                key = opt.flag or opt.dest
                action = actual[key]
                with self.subTest(command=command, flag=key):
                    self.assertEqual(
                        opt.kind == "positional", not action.option_strings
                    )
                    self.assertEqual(
                        opt.kind == "flag",
                        isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)),
                    )
                    self.assertEqual(
                        opt.kind == "list",
                        isinstance(action, argparse._AppendAction),
                    )
                    self.assertEqual(opt.kind == "choice", action.choices is not None)
                    if action.choices is not None:
                        self.assertEqual(
                            opt.choices,
                            tuple(str(c) for c in action.choices),
                            "the table's choices must match the parser's",
                        )

    def test_the_dest_matches_the_parser(self):
        from shepherd_dev.menu import COMMANDS, OPTIONS

        for command in COMMANDS:
            actual = _parser_options(command)
            for opt in OPTIONS[command]:
                with self.subTest(command=command, dest=opt.dest):
                    self.assertEqual(opt.dest, actual[opt.flag or opt.dest].dest)


if __name__ == "__main__":
    unittest.main()
