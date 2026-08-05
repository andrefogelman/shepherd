# Interactive Launch Menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bare `shepherd-dev` opens a menu over every subcommand except `mcp`, assembles the chosen command as argv, prints the equivalent command line, and runs it through the existing parser.

**Architecture:** A new `src/shepherd_dev/menu.py` turns interaction into argv and nothing else — it never imports the substrate, never calls `develop()`, never executes. `main()` gains a single guarded branch that calls it and feeds the result to today's `build_parser().parse_args(argv)`. A classification table names each flag `main` or `advanced`; a parser-driven drift test keeps that table from falling behind the CLI.

**Tech Stack:** Python 3.11+ stdlib only (`argparse`, `shlex`, `dataclasses`, `unittest`). No new dependency — the machine this runs on forbids installing anything.

**Spec:** `docs/superpowers/specs/2026-08-05-interactive-menu-design.md`

## Global Constraints

- No new third-party dependency. Interaction is `input()`; `curses` is explicitly out of scope.
- No existing subcommand's behavior, flags, or output may change. Every existing test must still pass unchanged.
- The menu appears **only** when `len(sys.argv) == 1 and sys.stdin.isatty()`. Piped/CI stdin keeps today's argparse usage error and exit 2.
- The menu builds **argv**, never an `argparse.Namespace`. Validation stays with the existing parser and validators.
- The classification table is exhaustive: every flag of every exposed subcommand is `main` or `advanced`. There is no hidden class.
- `mcp` is never listed.
- The menu reads `.shepherd-dev.json` (via `config`) but never writes it.
- `parser._actions` (private argparse API) may appear **only in tests**, never in `src/`.
- `q`, EOF, and Ctrl-C quit cleanly: exit 0, no traceback. Invalid input re-prompts, never raises.

## File Structure

| File | Responsibility |
|---|---|
| `src/shepherd_dev/menu.py` (new) | The whole menu: option table, argv construction, prompts, screens. One public function, `run_menu`. |
| `src/shepherd_dev/cli.py` (modify, `main()` only) | One guarded branch calling `run_menu`. Nothing else changes. |
| `tests/test_menu.py` (new) | Table drift, argv construction, prompt behavior, screen flow. |
| `tests/test_menu_cli.py` (new) | The `isatty` gate, end-to-end via subprocess. |
| `docs/MANUAL.md`, `docs/MANUAL.en.md` (modify) | Document the menu. |

`cli.py` is 2242 lines already; the menu is a separate concern with its own tests, so it gets its own module rather than growing that file further.

---

### Task 1: Option table + drift test

**Files:**
- Create: `src/shepherd_dev/menu.py`
- Test: `tests/test_menu.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `MAIN`/`ADVANCED` string constants; `Opt` frozen dataclass with fields `dest: str`, `kind: str`, `tier: str`, `choices: tuple[str, ...]`, `flag: str`; `OPTIONS: dict[str, tuple[Opt, ...]]` keyed by subcommand name; `COMMANDS: tuple[str, ...]` (menu order, excluding `mcp`). Tasks 2-4 all read these.

`kind` is one of: `"positional"`, `"flag"` (store_true), `"list"` (append), `"choice"`, `"value"`.

- [ ] **Step 1: Write the failing drift test**

```python
# tests/test_menu.py
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

    def test_每_kind_matches_the_parser(self):
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
                        isinstance(action, argparse._StoreTrueAction),
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
```

Rename `test_每_kind_matches_the_parser` to `test_each_kind_matches_the_parser` when you type it — the stray character above is a typo, not a requirement.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_menu -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shepherd_dev.menu'`

- [ ] **Step 3: Write the module and fill the table**

Create `src/shepherd_dev/menu.py`:

```python
"""Interactive launch menu: turns a bare `shepherd-dev` into argv.

This module knows how to ASK, not how to run. It never imports the
substrate, never calls develop(), and executes nothing — it hands back an
argv list that cli.main() feeds to the ordinary parser. Two things follow:
the existing validators still judge every value, and the "equivalent
command" the menu prints is the argv actually being run rather than a
reconstruction that could disagree with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAIN = "main"
ADVANCED = "advanced"


@dataclass(frozen=True)
class Opt:
    """One selectable setting of one subcommand.

    Mirrors what argparse already knows. The mirror is deliberate — reading
    argparse's private _actions from production code would break silently on
    an upgrade — and OptionTableDriftTests is what keeps the mirror honest.
    """

    dest: str
    kind: str  # positional | flag | list | choice | value
    tier: str = ADVANCED
    choices: tuple[str, ...] = ()
    flag: str = ""  # "" for positionals


#: Menu order. `mcp` is absent on purpose: it is a stdio server for another
#: program, and chosen from a terminal it would sit waiting for protocol
#: frames, looking hung.
COMMANDS = (
    "run", "run2", "runN",
    "settle", "settle-par",
    "init", "optimize", "status", "trace", "update",
)

#: Which commands sit under which heading on the first screen.
GROUPS = (
    ("develop", ("run", "run2", "runN")),
    ("decide", ("settle", "settle-par")),
    ("maintenance", ("init", "optimize", "status", "trace", "update")),
)

OPTIONS: dict[str, tuple[Opt, ...]] = {
    "run": (
        Opt(dest="feature", kind="positional", tier=MAIN),
        Opt(dest="provider", kind="choice", tier=MAIN, flag="--provider",
            choices=("claude", "static", "grok", "codex")),
        Opt(dest="mode", kind="choice", tier=MAIN, flag="--mode",
            choices=("feature", "tests")),
        Opt(dest="review_panel", kind="value", tier=MAIN, flag="--review-panel"),
        Opt(dest="review_rounds", kind="value", tier=MAIN, flag="--review-rounds"),
        Opt(dest="best_of", kind="choice", tier=MAIN, flag="--best-of",
            choices=("1", "2", "3", "4")),
        Opt(dest="review_report", kind="value", tier=MAIN, flag="--review-report"),
        # ... every remaining flag of `run` as tier=ADVANCED ...
    ),
    # ... one entry per command in COMMANDS ...
}
```

The table must end up exhaustive. Do not guess the flag list — derive it, once, and transcribe:

```bash
python3 -c "
import argparse
from shepherd_dev.cli import build_parser
subs=[a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)][0]
for name, sub in subs.choices.items():
    if name == 'mcp':
        continue
    for a in sub._actions:
        if isinstance(a, argparse._HelpAction):
            continue
        key = a.option_strings[-1] if a.option_strings else a.dest
        kind = ('positional' if not a.option_strings
                else 'flag' if isinstance(a, argparse._StoreTrueAction)
                else 'list' if isinstance(a, argparse._AppendAction)
                else 'choice' if a.choices is not None else 'value')
        print(f'{name}\t{key}\t{a.dest}\t{kind}\t{tuple(str(c) for c in a.choices) if a.choices else ()}')
"
```

`MAIN` for each command — every other flag is `ADVANCED`:

| Command | `MAIN` |
|---|---|
| `run` | `feature`, `--provider`, `--mode`, `--review-panel`, `--review-rounds`, `--best-of`, `--review-report` |
| `run2` | `feature_a`, `feature_b`, `--provider` |
| `runN` | `features`, `--provider`, `--max-workers` |
| `settle` | `run_ref`, `--reject` |
| `settle-par` | `proposal_id`, `--reject` |
| `init` | `--test-cmd`, `--review-panel` |
| `optimize` | `--apply` |
| `status` | (none) |
| `trace` | `run_id`, `--full` |
| `update` | (none) |

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_menu -v`
Expected: all 5 tests PASS. If `test_every_flag_is_classified` fails, the table is missing entries — add them; that failure IS the completeness spec.

- [ ] **Step 5: Confirm the drift test actually bites**

Delete one `Opt` line from the `run` table, re-run, confirm `test_every_flag_is_classified` fails naming `run`, then restore it. A guard that cannot fail is not a guard.

- [ ] **Step 6: Commit**

```bash
git add src/shepherd_dev/menu.py tests/test_menu.py
git commit -m "feat: option table for the launch menu, with a parser-driven drift test"
```

---

### Task 2: argv construction

**Files:**
- Modify: `src/shepherd_dev/menu.py`
- Test: `tests/test_menu.py` (append)

**Interfaces:**
- Consumes: `Opt`, `OPTIONS`, `MAIN`, `ADVANCED` (Task 1).
- Produces: `build_argv(command: str, values: dict[str, object]) -> list[str]` — `values` is keyed by `Opt.dest`; a `dest` absent from `values`, or holding `None`/`False`/`""`/`[]`, contributes nothing (the parser's own default then applies). Task 4 calls it.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_menu.BuildArgvTests -v`
Expected: FAIL with `ImportError: cannot import name 'build_argv'`

- [ ] **Step 3: Write the implementation**

Append to `src/shepherd_dev/menu.py`:

```python
def build_argv(command: str, values: dict[str, object]) -> list[str]:
    """Assemble the argv for one command from the chosen values.

    A dest that is absent, None, False, "" or [] contributes nothing, so the
    parser's own default applies — the menu never has to know what that
    default is, and can never drift from it.
    """
    argv: list[str] = [command]
    table = OPTIONS[command]
    for opt in (o for o in table if o.kind == "positional"):
        value = values.get(opt.dest)
        if value in (None, "", []):
            continue
        argv.extend(str(v) for v in value) if isinstance(value, list) else argv.append(str(value))
    for opt in (o for o in table if o.kind != "positional"):
        value = values.get(opt.dest)
        if value in (None, "", [], False):
            continue
        if opt.kind == "flag":
            argv.append(opt.flag)
        elif opt.kind == "list":
            for item in value:  # type: ignore[union-attr]
                argv.extend([opt.flag, str(item)])
        else:
            argv.extend([opt.flag, str(value)])
    return argv
```

Note the conditional-expression line is deliberately written as a statement; if your linter objects, use a plain `if/else` — behavior must be identical.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_menu -v`
Expected: all Task 1 + Task 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/shepherd_dev/menu.py tests/test_menu.py
git commit -m "feat: build_argv — menu choices to a parseable argv"
```

---

### Task 3: Prompt primitives

**Files:**
- Modify: `src/shepherd_dev/menu.py`
- Test: `tests/test_menu.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: three helpers Task 4 uses. Each returns `None` to mean "the user quit", which propagates up as a clean exit:
  - `ask_choice(prompt: str, n: int) -> int | None` — a 1-based index, or `None` on `q`/EOF/Ctrl-C. Re-prompts on anything else out of range.
  - `ask_text(prompt: str, default: str = "") -> str | None` — free text; empty input returns `default`; `None` on EOF/Ctrl-C.
  - `ask_value(opt: Opt, current: object) -> object | None` — one setting's value, honoring `opt.kind`: `flag` toggles, `choice` picks from `opt.choices`, `list` accepts comma-separated, otherwise free text. `None` on EOF/Ctrl-C.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_menu.PromptTests -v`
Expected: FAIL with `ImportError: cannot import name 'ask_choice'`

- [ ] **Step 3: Write the implementation**

Append to `src/shepherd_dev/menu.py`:

```python
def _read(prompt: str) -> str | None:
    """One line from the user. None means quit — EOF (a closed or piped
    stdin) and Ctrl-C both land here, and neither may raise past the menu."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def ask_choice(prompt: str, n: int) -> int | None:
    """A 1-based choice among n items. None = quit. Anything else re-asks."""
    while True:
        answer = _read(f"{prompt} [1-{n}, q quits]: ")
        if answer is None or answer.lower() == "q":
            return None
        if answer.isdigit() and 1 <= int(answer) <= n:
            return int(answer)


def ask_text(prompt: str, default: str = "") -> str | None:
    answer = _read(prompt)
    if answer is None:
        return None
    return answer or default


def ask_value(opt: Opt, current: object) -> object | None:
    if opt.kind == "flag":
        answer = _read(f"{opt.flag}: currently {bool(current)} — [enter] toggles, q quits: ")
        if answer is None or answer.lower() == "q":
            return None
        return not bool(current)
    if opt.kind == "choice":
        for i, choice in enumerate(opt.choices, 1):
            print(f"    {i}) {choice}")
        picked = ask_choice(f"  {opt.flag}", len(opt.choices))
        return None if picked is None else opt.choices[picked - 1]
    answer = _read(f"  {opt.flag} [{current if current not in (None, '', []) else 'unset'}]: ")
    if answer is None:
        return None
    if opt.kind == "list":
        return [part.strip() for part in answer.split(",") if part.strip()]
    return answer
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_menu -v`
Expected: all tests through Task 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/shepherd_dev/menu.py tests/test_menu.py
git commit -m "feat: EOF-safe prompt primitives for the launch menu"
```

---

### Task 4: The screens — `run_menu`

**Files:**
- Modify: `src/shepherd_dev/menu.py`
- Test: `tests/test_menu.py` (append)

**Interfaces:**
- Consumes: `COMMANDS`, `GROUPS`, `OPTIONS`, `MAIN`, `Opt` (Task 1); `build_argv` (Task 2); `ask_choice`, `ask_text`, `ask_value` (Task 3).
- Produces: `run_menu(argv_out: list[str]) -> int | None` — fills `argv_out` and returns `0`, or returns `None` when the user quit. Task 5 calls it from `main()`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_menu.RunMenuTests -v`
Expected: FAIL with `ImportError: cannot import name 'run_menu'`

- [ ] **Step 3: Write the implementation**

Append to `src/shepherd_dev/menu.py`:

```python
def _pick_command() -> str | None:
    """First screen: the subcommands, grouped."""
    print("\nShepherd — supervised AI development\n")
    ordered: list[str] = []
    for heading, names in GROUPS:
        print(f"  {heading}")
        for name in names:
            ordered.append(name)
            print(f"    {len(ordered)}) {name}")
        print()
    picked = ask_choice("choose", len(ordered))
    return None if picked is None else ordered[picked - 1]


def _prefill(command: str) -> dict[str, object]:
    """Values the repo already knows: read from config, never written back.
    A one-off menu choice must not silently become the repo's default."""
    from . import config

    values: dict[str, object] = {}
    repo_root = config.find_repo_root()
    if repo_root is None:
        return values
    saved = config.load_config(repo_root)
    for opt in OPTIONS[command]:
        if opt.dest in saved:
            values[opt.dest] = saved[opt.dest]
    return values


def _summary(command: str, values: dict[str, object], tier: str) -> None:
    shown = [o for o in OPTIONS[command] if o.tier == tier and o.kind != "positional"]
    for i, opt in enumerate(shown, 1):
        value = values.get(opt.dest)
        print(f"    {i}) {opt.flag:<22} {value if value not in (None, '', []) else '(default)'}")


def _edit_loop(command: str, values: dict[str, object]) -> bool:
    """Second and third screens. False means the user quit."""
    tier = MAIN
    while True:
        print(f"\n{command} — {tier} settings\n")
        _summary(command, values, tier)
        other = ADVANCED if tier == MAIN else MAIN
        answer = _read(f"\n  [enter] run · [e] edit · [a] {other} · [q] quit: ")
        if answer is None or answer.lower() == "q":
            return False
        if answer == "":
            return True
        if answer.lower() == "a":
            tier = other
            continue
        if answer.lower() == "e":
            shown = [o for o in OPTIONS[command] if o.tier == tier and o.kind != "positional"]
            if not shown:
                continue
            picked = ask_choice("  which", len(shown))
            if picked is None:
                return False
            opt = shown[picked - 1]
            new = ask_value(opt, values.get(opt.dest))
            if new is None:
                return False
            values[opt.dest] = new


def run_menu(argv_out: list[str]) -> int | None:
    """Fill argv_out with the argv the user assembled and return 0.
    Return None when the user quit — nothing should run."""
    command = _pick_command()
    if command is None:
        return None
    values = _prefill(command)
    for opt in (o for o in OPTIONS[command] if o.kind == "positional"):
        answer = ask_text(f"\n  {opt.dest}: ")
        if not answer:  # None (quit) or empty — never run on an empty required field
            return None
        values[opt.dest] = answer.split(",") if opt.dest == "features" else answer
    if not _edit_loop(command, values):
        return None
    argv_out.extend(build_argv(command, values))
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_menu -v`
Expected: all tests through Task 4 PASS

- [ ] **Step 5: Run the full existing suite**

Run: `python -m unittest discover -s tests`
Expected: OK — nothing outside `menu.py`/`test_menu.py` has been touched yet, so every pre-existing test must still pass at its previous count plus the new ones.

- [ ] **Step 6: Commit**

```bash
git add src/shepherd_dev/menu.py tests/test_menu.py
git commit -m "feat: the launch menu's three screens"
```

---

### Task 5: The `main()` hook and the equivalent command

**Files:**
- Modify: `src/shepherd_dev/cli.py` (`main()` only, around line 2210)
- Test: `tests/test_menu_cli.py` (create)

**Interfaces:**
- Consumes: `run_menu(argv_out) -> int | None` (Task 4).
- Produces: nothing later tasks consume — Task 6 is docs.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_menu_cli.py
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


class EquivalentCommandTests(unittest.TestCase):
    def test_the_printed_command_is_the_argv_that_runs(self):
        from shepherd_dev.cli import _equivalent_command

        line = _equivalent_command(["run", "add CPF validation", "--review-panel", "3"])
        self.assertIn("shepherd-dev run", line)
        self.assertIn("'add CPF validation'", line)  # shlex quotes the space
        self.assertIn("--review-panel 3", line)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_menu_cli -v`
Expected: `EquivalentCommandTests` FAILs with `ImportError: cannot import name '_equivalent_command'`. `IsattyGateTests` may already pass — that is correct and expected: it pins behavior that must not change.

- [ ] **Step 3: Write the implementation**

In `src/shepherd_dev/cli.py`, add above `main()`:

```python
def _equivalent_command(argv: list[str]) -> str:
    """The command line the menu just assembled, quoted so it can be pasted.

    Built from the argv actually being executed, so it can never disagree
    with what runs.
    """
    import shlex

    return "shepherd-dev " + shlex.join(argv)
```

Then change `main()`'s first line from `args = build_parser().parse_args()` to:

```python
def main() -> int:
    if len(sys.argv) == 1 and sys.stdin.isatty():
        # A bare, interactive invocation gets the menu. Piped or CI stdin
        # deliberately does not: it keeps today's usage error and exit 2, so
        # adding the menu changes nothing for existing automation.
        from . import menu

        argv: list[str] = []
        if menu.run_menu(argv) is None:
            return 0
        print(f"\nequivalent:\n  {_equivalent_command(argv)}\n")
        print("─" * 40)
        args = build_parser().parse_args(argv)
    else:
        args = build_parser().parse_args()
    ...  # the rest of main() is unchanged
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_menu_cli -v`
Expected: all 3 tests PASS

- [ ] **Step 5: Run the full existing suite**

Run: `python -m unittest discover -s tests`
Expected: OK. Pay attention to any pre-existing test that invokes the CLI as a subprocess — they pass arguments, so they take the `else` branch untouched.

- [ ] **Step 6: Commit**

```bash
git add src/shepherd_dev/cli.py tests/test_menu_cli.py
git commit -m "feat: open the launch menu on a bare interactive shepherd-dev"
```

---

### Task 6: Docs

**Files:**
- Modify: `docs/MANUAL.md`, `docs/MANUAL.en.md`

**Interfaces:**
- Consumes: nothing. Produces: nothing.

- [ ] **Step 1: Find where each manual introduces the CLI**

Run: `grep -n "shepherd-dev run" docs/MANUAL.md docs/MANUAL.en.md | head`

- [ ] **Step 2: Add a section near the top of each**

In `docs/MANUAL.en.md`, before the first `shepherd-dev run` usage, add:

```markdown
### Starting without remembering the flags

Run `shepherd-dev` with no arguments in a terminal and it opens a menu:
every subcommand, then that subcommand's settings — the common ones first,
the rest behind `[a]`. Nothing is withheld from the menu.

It prints the equivalent command line before running, so the menu is also
how you learn (or assemble) the command to paste into a script:

    equivalent:
      shepherd-dev run 'add CPF validation' --review-panel 3

The menu reads this repo's saved defaults but never writes them — a choice
made in the menu applies to that run only. `shepherd-dev init` remains the
place to change a repo's defaults.

Piped or non-interactive stdin never gets the menu: a bare `shepherd-dev`
in CI keeps printing the usage error, exactly as before.
```

Translate the same section into Portuguese for `docs/MANUAL.md`, matching that file's existing tone and its established vocabulary (it already uses `portão`, `revisor`, `achado`, `ledger de achados`).

- [ ] **Step 3: Commit**

```bash
git add docs/MANUAL.md docs/MANUAL.en.md
git commit -m "docs: document the interactive launch menu"
```

---

## Self-Review

**Spec coverage:** every section of the spec maps to a task —

| Spec section | Task |
|---|---|
| Trigger (bare, `isatty`-gated) | 5 |
| Setting scope (main + advanced) | 1 (table), 4 (screens) |
| On confirm: run **and** print equivalent | 5 |
| Coverage (all but `mcp`) | 1 (`COMMANDS`, drift test asserts `mcp` absent) |
| Input: numbered + `input()` | 3 |
| Persistence: read, never write | 4 (`_prefill`) |
| Architecture: argv not Namespace | 2, 5 |
| Option table exhaustive, no hidden class | 1 |
| Drift test, `_actions` in tests only | 1 |
| Edge behavior (invalid re-prompts, q/EOF/Ctrl-C) | 3, 4 |
| Testing (5 named kinds) | 1-5 |
| Out of scope | nothing implements these, by construction |

**Placeholder scan:** the only ellipses are inside Task 1's Step 3 table, which is explicitly completed by a derivation command in that same step and enforced by Task 1's own drift test — the test failure is the completeness spec, not a deferred decision. No TBD/TODO elsewhere.

**Type consistency:** `Opt(dest, kind, tier, choices, flag)` is defined in Task 1 and used with those exact field names in Tasks 2, 3, 4. `build_argv(command, values)` is defined in Task 2 and called with that signature in Task 4. `run_menu(argv_out)` is defined in Task 4 and called with that signature in Task 5. `MAIN`/`ADVANCED` are the two tier values throughout. `kind` values are the same five strings in Tasks 1, 2, 3.

**One gap found and fixed while reviewing:** Task 4's `run_menu` splits `features` on commas for `runN` (whose positional is `nargs="+"`), which Task 2's `test_nargs_positional_expands` expects as a list. Without that branch the list would arrive as a single string and `runN` would receive one feature instead of several. The branch is in the Task 4 code above.

---

**Plan complete and saved to `docs/superpowers/plans/2026-08-05-interactive-menu.md`.** Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
