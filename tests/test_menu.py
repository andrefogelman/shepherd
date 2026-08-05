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
                    if opt.kind == "positional":
                        self.assertEqual(
                            opt.nargs, action.nargs or "",
                            "nargs must mirror the parser's arity ('?' for an "
                            "optional positional like trace's run_id, '+' for "
                            "one-or-more like runN's features) so a positional "
                            "whose arity changes is caught here rather than "
                            "silently cancelling or mishandling the menu.",
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

    def test_a_positional_value_starting_with_dash_gets_a_separator(self):
        """I4 regression: free text beginning with '-' (a plausible feature
        request like "--dry-run support") would otherwise be read as a flag
        by argparse, since positionals are emitted with no `--` separator.
        build_argv must insert one whenever a positional value starts with
        '-', with the parsed positional equal to what was chosen."""
        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import build_argv

        argv = build_argv("run", {"feature": "--dry-run support", "review_panel": 3})
        self.assertEqual(argv, ["run", "--review-panel", "3", "--", "--dry-run support"])
        args = build_parser().parse_args(argv)
        self.assertEqual(args.feature, "--dry-run support")
        self.assertEqual(args.review_panel, 3)

    def test_a_normal_positional_value_gets_no_separator(self):
        """The common case (no leading dash) keeps today's shape — no `--`
        clutter for the vast majority of feature requests."""
        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import build_argv

        argv = build_argv("run", {"feature": "add CPF validation", "review_panel": 3})
        self.assertNotIn("--", argv)
        self.assertEqual(argv, ["run", "add CPF validation", "--review-panel", "3"])
        args = build_parser().parse_args(argv)
        self.assertEqual(args.feature, "add CPF validation")

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


class RunMenuTests(unittest.TestCase):
    def test_quitting_the_first_screen_returns_none_and_writes_nothing(self):
        from unittest.mock import patch

        from shepherd_dev.menu import run_menu

        argv: list[str] = []
        with patch("builtins.input", return_value="q"):
            self.assertIsNone(run_menu(argv))
        self.assertEqual(argv, [])

    def test_a_run_with_defaults_produces_a_parseable_argv(self):
        from unittest.mock import patch

        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import COMMANDS, run_menu

        # 1) pick `run`  2) type the feature  3) [enter] to run
        answers = [str(COMMANDS.index("run") + 1), "add CPF validation", ""]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertEqual(argv[:2], ["run", "add CPF validation"])
        build_parser().parse_args(argv)  # must parse

    def test_an_empty_feature_cancels_rather_than_running_empty(self):
        from unittest.mock import patch

        from shepherd_dev.menu import COMMANDS, run_menu

        answers = [str(COMMANDS.index("run") + 1), ""]  # pick run, then blank feature
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertIsNone(run_menu(argv))
        self.assertEqual(argv, [])

    def test_eof_anywhere_quits_without_raising(self):
        from unittest.mock import patch

        from shepherd_dev.menu import run_menu

        argv: list[str] = []
        with patch("builtins.input", side_effect=EOFError):
            self.assertIsNone(run_menu(argv))
        self.assertEqual(argv, [])

    def test_a_command_with_no_required_input_goes_straight_through(self):
        from unittest.mock import patch

        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import COMMANDS, run_menu

        answers = [str(COMMANDS.index("status") + 1), ""]  # pick status, [enter] runs
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertEqual(argv, ["status"])
        build_parser().parse_args(argv)

    def test_editing_a_main_setting_reaches_the_argv(self):
        from unittest.mock import patch

        from shepherd_dev.menu import COMMANDS, OPTIONS, MAIN, run_menu

        main_run = [o for o in OPTIONS["run"] if o.tier == MAIN and o.kind != "positional"]
        panel_pos = next(i for i, o in enumerate(main_run, 1) if o.dest == "review_panel")
        answers = [
            str(COMMANDS.index("run") + 1),  # pick run
            "add X",                          # the feature
            "e",                              # edit a field
            str(panel_pos),                   # pick --review-panel
            "3",                              # its new value
            "",                               # [enter] runs
        ]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertIn("--review-panel", argv)
        self.assertEqual(argv[argv.index("--review-panel") + 1], "3")

    def test_a_blank_re_edit_of_a_value_field_leaves_it_unchanged(self):
        """Decision (Task 4): ask_value's free-text branch returns "" on a
        bare Enter rather than echoing back the current value the way
        ask_text does. A naive edit loop would then blank a field the user
        merely reopened to look at. _edit_loop treats that "" as "leave
        unchanged" instead — this pins the round trip: set review_rounds to
        7, reopen the same field, hit Enter with nothing typed, and confirm
        7 survives into the argv rather than being cleared."""
        from unittest.mock import patch

        from shepherd_dev.menu import COMMANDS, OPTIONS, MAIN, run_menu

        main_run = [o for o in OPTIONS["run"] if o.tier == MAIN and o.kind != "positional"]
        rounds_pos = next(i for i, o in enumerate(main_run, 1) if o.dest == "review_rounds")
        answers = [
            str(COMMANDS.index("run") + 1),  # pick run
            "add X",                          # the feature
            "e",                              # edit a field
            str(rounds_pos),                  # pick --review-rounds
            "7",                              # set it
            "e",                              # edit again
            str(rounds_pos),                  # pick --review-rounds again
            "",                               # bare enter — must NOT blank it
            "",                               # [enter] runs
        ]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertIn("--review-rounds", argv)
        self.assertEqual(argv[argv.index("--review-rounds") + 1], "7")

    def test_a_blank_re_edit_of_a_list_field_leaves_it_unchanged(self):
        """Same decision, extended to kind="list": ask_value returns [] on a
        bare Enter for a list field too, so the same "leave unchanged"
        treatment applies there for the same reason."""
        from unittest.mock import patch

        from shepherd_dev.menu import ADVANCED, COMMANDS, OPTIONS, run_menu

        advanced_run = [o for o in OPTIONS["run"] if o.tier == ADVANCED and o.kind != "positional"]
        prefix_pos = next(i for i, o in enumerate(advanced_run, 1) if o.dest == "allowed_prefix")
        answers = [
            str(COMMANDS.index("run") + 1),  # pick run
            "add X",                          # the feature
            "a",                              # switch to advanced settings
            "e",                              # edit a field
            str(prefix_pos),                  # pick --allowed-prefix
            "src/, tests/",                   # set it
            "e",                              # edit again
            str(prefix_pos),                  # pick --allowed-prefix again
            "",                               # bare enter — must NOT clear it
            "",                               # [enter] runs
        ]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertEqual(
            argv.count("--allowed-prefix"), 2,
            "the list set before the blank re-edit must survive intact",
        )
        self.assertIn("src/", argv)
        self.assertIn("tests/", argv)

    def test_a_dash_explicitly_clears_a_value_field(self):
        """Bare Enter means "leave unchanged" (see the two tests above), so
        there has to be a SEPARATE, deliberate way to clear a field back to
        unset — e.g. to drop a value _prefill pulled in from the repo's
        saved config for this one run. "-" is that sentinel: set
        review_rounds to 7, reopen the field, type "-", and confirm
        --review-rounds is absent from argv entirely (not merely re-set to
        7) and that the parser's own default (1) is what's parsed."""
        from unittest.mock import patch

        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import COMMANDS, OPTIONS, MAIN, run_menu

        main_run = [o for o in OPTIONS["run"] if o.tier == MAIN and o.kind != "positional"]
        rounds_pos = next(i for i, o in enumerate(main_run, 1) if o.dest == "review_rounds")
        answers = [
            str(COMMANDS.index("run") + 1),  # pick run
            "add X",                          # the feature
            "e",                              # edit a field
            str(rounds_pos),                  # pick --review-rounds
            "7",                              # set it
            "e",                              # edit again
            str(rounds_pos),                  # pick --review-rounds again
            "-",                              # explicit clear
            "",                               # [enter] runs
        ]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertNotIn("--review-rounds", argv)
        args = build_parser().parse_args(argv)
        self.assertEqual(args.review_rounds, 1)  # the parser's own default

    def test_a_dash_explicitly_clears_a_list_field(self):
        """Same sentinel, for kind="list": set allowed_prefix, reopen it,
        type "-", and confirm --allowed-prefix is absent from argv and the
        parser's own default ([]) is what's parsed."""
        from unittest.mock import patch

        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import ADVANCED, COMMANDS, OPTIONS, run_menu

        advanced_run = [o for o in OPTIONS["run"] if o.tier == ADVANCED and o.kind != "positional"]
        prefix_pos = next(i for i, o in enumerate(advanced_run, 1) if o.dest == "allowed_prefix")
        answers = [
            str(COMMANDS.index("run") + 1),  # pick run
            "add X",                          # the feature
            "a",                              # switch to advanced settings
            "e",                              # edit a field
            str(prefix_pos),                  # pick --allowed-prefix
            "src/, tests/",                   # set it
            "e",                              # edit again
            str(prefix_pos),                  # pick --allowed-prefix again
            "-",                              # explicit clear
            "",                               # [enter] runs
        ]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertNotIn("--allowed-prefix", argv)
        args = build_parser().parse_args(argv)
        self.assertEqual(args.allowed_prefix, [])  # the parser's own default

    def test_multi_feature_input_strips_whitespace_around_each_part(self):
        """A bare answer.split(",") let whitespace after a comma survive into
        the feature text ('add X, add Y' -> [..., ' add Y']). Must match
        ask_value's list branch, which already strips."""
        from unittest.mock import patch

        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import COMMANDS, run_menu

        answers = [str(COMMANDS.index("runN") + 1), "add X, add Y", ""]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertEqual(argv[:3], ["runN", "add X", "add Y"])
        args = build_parser().parse_args(argv)
        self.assertEqual(args.features, ["add X", "add Y"])

    def test_multi_feature_input_drops_empty_entries_between_commas(self):
        """'a,,b' must not hand the worker an empty feature request."""
        from unittest.mock import patch

        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import COMMANDS, run_menu

        answers = [str(COMMANDS.index("runN") + 1), "a,,b", ""]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        args = build_parser().parse_args(argv)
        self.assertEqual(args.features, ["a", "b"])

    def test_a_trailing_comma_yields_one_feature_not_an_empty_one(self):
        """'a, ' has one real feature after stripping the trailing empty
        part. That is NOT the "nothing survived" case (covered separately
        below) — verified against the real parser rather than assumed:
        `features` is nargs="+" (>=1) at the argparse level, so a single
        feature parses; runN's own 2-5 floor is a separate runtime check in
        cmd_runN, not something parse_args enforces or the menu duplicates."""
        from unittest.mock import patch

        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import COMMANDS, run_menu

        answers = [str(COMMANDS.index("runN") + 1), "a, ", ""]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        args = build_parser().parse_args(argv)
        self.assertEqual(args.features, ["a"])

    def test_all_commas_cancels_rather_than_running_empty(self):
        """',' alone strips down to no features at all — the same "empty
        required field" rule that already cancels on a bare Enter."""
        from unittest.mock import patch

        from shepherd_dev.menu import COMMANDS, run_menu

        answers = [str(COMMANDS.index("runN") + 1), ","]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertIsNone(run_menu(argv))
        self.assertEqual(argv, [])

    def test_toggling_the_no_verbose_row_sets_verbose_false(self):
        """I2 regression: choosing the --no-verbose row must produce
        verbose=False (and --no-verbose in argv), not --verbose. ask_value
        must present a negating row in its own terms (on = current is
        False) rather than toggling the shared dest's raw boolean."""
        from unittest.mock import patch

        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import ADVANCED, COMMANDS, OPTIONS, run_menu

        advanced_run = [o for o in OPTIONS["run"] if o.tier == ADVANCED and o.kind != "positional"]
        no_verbose_pos = next(
            i for i, o in enumerate(advanced_run, 1) if o.dest == "verbose" and o.negates
        )
        answers = [
            str(COMMANDS.index("run") + 1),  # pick run
            "add X",                          # the feature
            "a",                              # switch to advanced settings
            "e",                              # edit a field
            str(no_verbose_pos),              # pick --no-verbose
            "",                                # toggle it
            "",                               # [enter] runs
        ]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertIn("--no-verbose", argv)
        self.assertNotIn("--verbose", argv)
        args = build_parser().parse_args(argv)
        self.assertFalse(args.verbose)

    def test_toggling_the_verbose_row_sets_verbose_true(self):
        """The non-negating --verbose row must still toggle to True from
        its default, unaffected by the I2 fix above."""
        from unittest.mock import patch

        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import ADVANCED, COMMANDS, OPTIONS, run_menu

        advanced_run = [o for o in OPTIONS["run"] if o.tier == ADVANCED and o.kind != "positional"]
        verbose_pos = next(
            i for i, o in enumerate(advanced_run, 1) if o.dest == "verbose" and not o.negates
        )
        answers = [
            str(COMMANDS.index("run") + 1),  # pick run
            "add X",                          # the feature
            "a",                              # switch to advanced settings
            "e",                              # edit a field
            str(verbose_pos),                 # pick --verbose
            "",                                # toggle it
            "",                               # [enter] runs
        ]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertIn("--verbose", argv)
        self.assertNotIn("--no-verbose", argv)
        args = build_parser().parse_args(argv)
        self.assertTrue(args.verbose)

    def test_trace_with_a_blank_run_id_uses_the_parsers_default(self):
        """I3 regression: run_id is nargs='?' (default "last") — a bare
        `shepherd-dev trace` (replay the most recent run) is its common
        form. A blank answer must leave the positional unset so the
        parser's own default applies, not cancel the whole menu."""
        from unittest.mock import patch

        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import COMMANDS, run_menu

        # pick trace, blank run_id, [enter] runs
        answers = [str(COMMANDS.index("trace") + 1), "", ""]
        argv: list[str] = []
        with patch("builtins.input", side_effect=answers):
            self.assertEqual(run_menu(argv), 0)
        self.assertEqual(argv, ["trace"])
        args = build_parser().parse_args(argv)
        self.assertEqual(args.run_id, "last")


if __name__ == "__main__":
    unittest.main()
