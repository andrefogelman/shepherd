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
                    if opt.kind == "flag":
                        self.assertEqual(
                            opt.negates,
                            isinstance(action, argparse._StoreFalseAction),
                            "negates must be True exactly for the store_false half "
                            "of a store_true/store_false pair",
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


class BuildArgvTests(unittest.TestCase):
    def test_positional_then_flags(self):
        from shepherd_dev.menu import build_argv

        argv = build_argv("run", {"feature": "add CPF validation", "review_panel": 3})
        self.assertEqual(argv, ["run", "add CPF validation", "--review-panel", "3"])

    def test_unset_values_contribute_nothing(self):
        from shepherd_dev.menu import build_argv

        argv = build_argv(
            "run",
            {"feature": "add X", "provider": None, "review_report": "", "best_of": None},
        )
        self.assertEqual(argv, ["run", "add X"])

    def test_store_true_emits_the_bare_flag(self):
        from shepherd_dev.menu import build_argv

        self.assertEqual(build_argv("settle", {"run_ref": "run-abc", "reject": True}),
                         ["settle", "run-abc", "--reject"])

    def test_store_true_false_emits_nothing(self):
        from shepherd_dev.menu import build_argv

        self.assertEqual(build_argv("settle", {"run_ref": "run-abc", "reject": False}),
                         ["settle", "run-abc"])

    def test_append_flags_repeat(self):
        from shepherd_dev.menu import build_argv

        argv = build_argv("run", {"feature": "add X", "allowed_prefix": ["src/", "tests/"]})
        self.assertEqual(
            argv, ["run", "add X", "--allowed-prefix", "src/", "--allowed-prefix", "tests/"]
        )

    def test_nargs_positional_expands(self):
        """runN takes 2-5 features as one nargs='+' positional."""
        from shepherd_dev.menu import build_argv

        argv = build_argv("runN", {"features": ["add X", "add Y"]})
        self.assertEqual(argv, ["runN", "add X", "add Y"])

    def test_store_true_and_store_false_share_a_dest_without_double_firing(self):
        """`verbose` has two table entries (--verbose store_true, --no-verbose
        store_false, negates=True) sharing one dest on run, run2 and runN.
        A naive "emit the flag whenever the value isn't unset" implementation
        fires BOTH for verbose=True, and since --no-verbose is applied last,
        argparse silently parses that back to verbose=False — command still
        matches, no SystemExit, but the round-tripped value is wrong. All
        three commands also default verbose to True, so an implementation
        that treats False as merely "unset" can never turn verbose off at
        all. This pins the full round trip (chosen value -> argv -> parsed
        value) for True, False and absent, on all three commands."""
        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import build_argv

        base_values = {
            "run": {"feature": "add X"},
            "run2": {"feature_a": "a", "feature_b": "b"},
            "runN": {"features": ["a", "b"]},
        }
        for command, base in base_values.items():
            for chosen in (True, False, None):
                values = dict(base)
                if chosen is not None:
                    values["verbose"] = chosen
                with self.subTest(command=command, chosen=chosen):
                    argv = build_argv(command, values)
                    self.assertLessEqual(
                        sum(f in argv for f in ("--verbose", "--no-verbose")), 1,
                        "at most one of --verbose/--no-verbose may fire",
                    )
                    args = build_parser().parse_args(argv)
                    expected = True if chosen is None else chosen  # default is True
                    self.assertEqual(args.verbose, expected)

    def test_every_built_argv_parses(self):
        """The loop-closer: the menu must not be able to emit a command its
        own CLI would reject."""
        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import build_argv

        cases = [
            ("run", {"feature": "add X", "provider": "static", "review_panel": 2}),
            ("run2", {"feature_a": "a", "feature_b": "b"}),
            ("runN", {"features": ["a", "b"]}),
            ("settle", {"run_ref": "run-abc", "reject": True}),
            ("status", {}),
            ("update", {}),
        ]
        for command, values in cases:
            with self.subTest(command=command):
                argv = build_argv(command, values)
                args = build_parser().parse_args(argv)  # raises SystemExit on a bad argv
                self.assertEqual(args.command, command)


class PromptTests(unittest.TestCase):
    """input() is the repo's established interaction primitive (_ask_decision,
    _ask_review_panel). These follow the same EOF-safe shape."""

    def test_choice_returns_a_one_based_index(self):
        from unittest.mock import patch

        from shepherd_dev.menu import ask_choice

        with patch("builtins.input", return_value="2"):
            self.assertEqual(ask_choice("pick", 3), 2)

    def test_choice_quits_on_q_eof_and_interrupt(self):
        from unittest.mock import patch

        from shepherd_dev.menu import ask_choice

        for side in ("q", EOFError, KeyboardInterrupt):
            with self.subTest(side=side):
                kw = {"return_value": side} if isinstance(side, str) else {"side_effect": side}
                with patch("builtins.input", **kw):
                    self.assertIsNone(ask_choice("pick", 3))

    def test_choice_reprompts_on_garbage_and_out_of_range(self):
        from unittest.mock import patch

        from shepherd_dev.menu import ask_choice

        with patch("builtins.input", side_effect=["banana", "0", "9", "1"]) as m:
            self.assertEqual(ask_choice("pick", 3), 1)
        self.assertEqual(m.call_count, 4)

    def test_choice_reprompts_on_a_non_decimal_unicode_digit(self):
        """str.isdigit() is True for '²' but int('²') raises ValueError.
        Must re-prompt, never raise past ask_choice."""
        from unittest.mock import patch

        from shepherd_dev.menu import ask_choice

        with patch("builtins.input", side_effect=["²", "1"]) as m:
            self.assertEqual(ask_choice("pick", 3), 1)
        self.assertEqual(m.call_count, 2)

    def test_choice_with_zero_items_quits_without_asking(self):
        from unittest.mock import patch

        from shepherd_dev.menu import ask_choice

        with patch("builtins.input") as m:
            self.assertIsNone(ask_choice("pick", 0))
        m.assert_not_called()

    def test_text_empty_keeps_the_default(self):
        from unittest.mock import patch

        from shepherd_dev.menu import ask_text

        with patch("builtins.input", return_value=""):
            self.assertEqual(ask_text("feature", default="keep me"), "keep me")

    def test_text_quits_on_eof(self):
        from unittest.mock import patch

        from shepherd_dev.menu import ask_text

        with patch("builtins.input", side_effect=EOFError):
            self.assertIsNone(ask_text("feature"))

    def test_value_toggles_a_store_true_flag(self):
        from unittest.mock import patch

        from shepherd_dev.menu import Opt, ask_value

        opt = Opt(dest="reject", kind="flag", flag="--reject")
        with patch("builtins.input", return_value=""):
            self.assertIs(ask_value(opt, current=False), True)
        with patch("builtins.input", return_value=""):
            self.assertIs(ask_value(opt, current=True), False)

    def test_value_picks_from_choices(self):
        from unittest.mock import patch

        from shepherd_dev.menu import Opt, ask_value

        opt = Opt(dest="provider", kind="choice", flag="--provider",
                  choices=("claude", "static", "grok", "codex"))
        with patch("builtins.input", return_value="2"):
            self.assertEqual(ask_value(opt, current="claude"), "static")

    def test_value_splits_a_list_on_commas(self):
        from unittest.mock import patch

        from shepherd_dev.menu import Opt, ask_value

        opt = Opt(dest="allowed_prefix", kind="list", flag="--allowed-prefix")
        with patch("builtins.input", return_value="src/, tests/"):
            self.assertEqual(ask_value(opt, current=[]), ["src/", "tests/"])


if __name__ == "__main__":
    unittest.main()
