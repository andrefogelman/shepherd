# Interactive launch menu — design

**Status:** approved 2026-08-05. Next step: implementation plan.

## Problem

`shepherd-dev` exposes 11 subcommands (10 of them user-facing; `mcp` is a
machine interface). `run` alone carries 33 flags, `run2` 21, `runN` 16. Everything the tool can do is reachable only by already
knowing the flag name — including the features most worth reaching for
(`--review-panel`, `--best-of`, `--review-rounds`). Today a bare
`shepherd-dev` with no arguments prints an argparse usage error and exits 2.

## Goal

A bare `shepherd-dev` opens a menu that presents every runnable mode and
every initial-call setting as a selectable option, runs the chosen command,
and shows the equivalent command line so the menu teaches the CLI instead of
replacing it.

## Decisions

Each of these was chosen deliberately over the alternative noted.

| Decision | Chosen | Over |
|---|---|---|
| Trigger | bare `shepherd-dev` (no args) | a `menu` subcommand; a `--menu` flag |
| Setting scope | curated main screen + an "advanced" branch holding the rest | exhaustive (every flag every time); curated-only (no escape) |
| On confirm | run it **and** print the equivalent command | run silently; print-only |
| Coverage | every subcommand except `mcp` | run-family only; run-family + settle |
| Input | numbered choice + `input()` | arrow-key navigation via `curses` |
| Persistence | read from `.shepherd-dev.json`, never write | prompt to save; always save |

Notes on the two least obvious:

**`mcp` is excluded** because it is a stdio server for another program. Chosen
from an interactive menu it would sit waiting for protocol frames on a
terminal, looking hung.

**Never writing config** keeps a one-off experiment from silently becoming the
repo's default. `init` remains the only place a persistent default is set.
This is the same failure the final review caught in `cmd_init` itself, where a
re-run silently reset a saved `review_panel` to 1.

## Architecture

A new module, `src/shepherd_dev/menu.py`, with one responsibility: **turn
interaction into argv**. It does not know about `develop()`, the substrate, or
any command's behavior, and it executes nothing.

```python
def run_menu(argv_out: list[str]) -> int | None:
    """Fill argv_out with the argv the user assembled and return 0.
    Return None when the user quit (q / EOF / Ctrl-C) — nothing should run."""
```

`cli.py` is already 2242 lines; the menu is a separate concern with its own
tests, so it gets its own file.

### Hook

In `main()` (`src/shepherd_dev/cli.py:2210`), before parsing:

```python
def main() -> int:
    if len(sys.argv) == 1 and sys.stdin.isatty():
        from . import menu

        argv: list[str] = []
        if menu.run_menu(argv) is None:
            return 0            # user quit: nothing runs, clean exit
        args = build_parser().parse_args(argv)
    else:
        args = build_parser().parse_args()
    ...                          # everything below is today's path, unchanged
```

Two properties follow from building argv rather than a `Namespace`:

1. **Existing validation still applies, unchanged.** `--review-panel 9` is
   refused by the same `_validate_review_panel` every other invocation hits —
   not by a second check inside the menu that could drift from it.
2. **The equivalent command is correct by construction.** It is `shlex.join`
   of the argv actually being executed, not a reconstruction that could
   disagree with what ran.

Building a `Namespace` directly was rejected for a concrete reason: it must
carry every attribute the downstream code reads (33 for `run`), and a missing
one fails at `args.X` deep inside execution. That exact bug already occurred
in this codebase — `tests/test_perf.py`'s `StartupOverlapTests` hand-builds a
`Namespace` and broke when `_cmd_run_inner` began reading `args.review_panel`.

### Screens

All three are numbered lists read with `input()`.

1. **Subcommand** — the 10 commands grouped as *develop* (`run`, `run2`,
   `runN`), *decide* (`settle`, `settle-par`), *maintenance* (`init`,
   `optimize`, `status`, `trace`, `update`). The *decide* entries carry a live
   pending count, read the way `cmd_status` already reads it
   (`cli.py:1841-1847`, iterating `PROPOSALS_DIR`) plus `runs_status` for
   retained runs.

2. **Editable summary** — the chosen subcommand's `MAIN` settings, each with
   its current value and where that value came from (`default`, `from repo`,
   `chosen`). Keys: `[enter]` run, `[e]` edit a field, `[a]` advanced,
   `[q]` quit.

3. **Advanced** — the same screen over that subcommand's `ADVANCED` settings.

A required free-text field (a feature request) is read with `input()`; empty
input cancels rather than running with an empty feature.

## Option table

`menu.py` holds a table classifying every flag of every exposed subcommand as
either `MAIN` or `ADVANCED`. The classification is exhaustive: **no flag is
withheld from the menu**.

For `run`, `MAIN` is: the `feature` positional, `--provider`, `--mode`,
`--review-panel`, `--review-rounds`, `--best-of`, `--review-report`.
Everything else is `ADVANCED`.

An earlier draft added a third class, `HIDDEN`, for `--json` and `--quiet` on
the reasoning that neither makes sense to someone sitting at a menu. That was
wrong twice over, and the class is dropped:

- The menu prints the equivalent command, which makes it a command *builder*
  as much as a runner. Someone assembling a line to paste into CI wants
  `--json` specifically, and `--quiet` for exactly the same reason.
- "Curated + advanced" was chosen because nothing becomes unreachable. A
  hidden class breaks that promise.

Neither flag is broken when chosen from a menu — `--json` prints its envelope
as the last stdout line and skips the interactive settle (`cli.py:1997-2001`),
`--quiet` swaps in `NullProgress` (`cli.py:975`). Both are merely unusual, and
unusual is what `ADVANCED` is for.

Two `ADVANCED` entries are pre-filled rather than blank: `--repo` from
`config.find_repo_root()`, and `--test-cmd` from `config.resolve_test_cmd()`,
each displaying its origin (`saved` / `detected` / `native`).

### Keeping the table honest

A drift test enumerates each subparser's real flags and asserts the table
classifies all of them as `MAIN` or `ADVANCED`. A flag added later is in
neither and fails the test until someone classifies it — the same guard, and
the same reasoning, as `RendererDriftTests` in `tests/test_supervisor.py`.

That enumeration needs `parser._actions`, which is private argparse API. **It
lives in the test only**; production code reads the table. If argparse changes
its internals, a test fails loudly rather than shipping broken behavior.

## Edge behavior

**Non-interactive stdin does not get a menu.** The `sys.stdin.isatty()` gate in
the hook means a bare `shepherd-dev` in CI, or with stdin piped, still gets
today's argparse usage error and exit 2. Without this, choosing "no arguments"
as the trigger would silently change behavior for existing automation — the
exact thing that choice was meant to avoid.

- Invalid input re-prompts; it never raises.
- `q`, EOF, and Ctrl-C all quit cleanly: exit 0, no traceback. Same shape as
  the existing `_ask_decision` (`cli.py:373`) and `_ask_review_panel`.
- With no enclosing Shepherd workspace, the menu still opens — `init` is one
  of its entries — but the run-family entries say so.

## Testing

Argv construction is a pure function, so most coverage needs no terminal:

- **Table drift** — parser-driven; an unclassified flag fails.
- **Argv construction** — choices produce the expected argv, *and* that argv
  parses under `build_parser()` without error. This closes the loop: the menu
  cannot emit a command its own CLI would reject.
- **The `isatty` gate** — piped stdin yields the usage error, not a menu.
- **Input handling** — EOF, Ctrl-C, empty, and garbage each behave as
  specified, following `_ask_review_panel`'s existing test shape.
- **One end-to-end** — a subprocess with piped stdin, asserting the usage
  error and no menu output.

## Out of scope

- Changing any existing subcommand's behavior, flags, or output.
- Persisting menu choices (see Decisions).
- `mcp` (see Decisions).
- Arrow-key/`curses` navigation.
