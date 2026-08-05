"""Interactive launch menu: turns a bare `shepherd-dev` into argv.

This module knows how to ASK, not how to run. It never imports the
substrate, never calls develop(), and executes nothing — it hands back an
argv list that cli.main() feeds to the ordinary parser. Two things follow:
the existing validators still judge every value, and the "equivalent
command" the menu prints is the argv actually being run rather than a
reconstruction that could disagree with it.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    #: True only for the store_false half of a store_true/store_false pair
    #: sharing one dest (currently just the three `--no-verbose` entries).
    #: Such a `kind="flag"` entry fires on value is False; every other
    #: flag fires on value is True. OptionTableDriftTests checks this
    #: against the real action, so a wrong setting fails loudly.
    negates: bool = False
    #: argparse's `nargs` for a positional, mirrored as a string: "" for a
    #: plain required single value (argparse's nargs=None), "+" for
    #: one-or-more (runN's `features`), "?" for optional-with-a-default
    #: (trace's `run_id`). Ignored for kind != "positional".
    #: OptionTableDriftTests checks this against the real action's nargs,
    #: so a positional whose arity changes is caught here rather than
    #: silently cancelling the menu or losing the special-case handling.
    nargs: str = ""


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
        Opt(dest="repo", kind="value", flag="--repo"),
        Opt(dest="test_cmd", kind="value", flag="--test-cmd"),
        Opt(dest="provider", kind="choice", tier=MAIN, flag="--provider",
            choices=("claude", "static", "grok", "codex")),
        Opt(dest="worker_backend", kind="choice", flag="--worker-backend",
            choices=("auto", "host", "lane")),
        Opt(dest="grok_cmd", kind="value", flag="--grok-cmd"),
        Opt(dest="grok_model", kind="value", flag="--grok-model"),
        Opt(dest="codex_cmd", kind="value", flag="--codex-cmd"),
        Opt(dest="codex_model", kind="value", flag="--codex-model"),
        Opt(dest="mode", kind="choice", tier=MAIN, flag="--mode",
            choices=("feature", "tests")),
        Opt(dest="no_review", kind="flag", flag="--no-review"),
        Opt(dest="auto_settle", kind="flag", flag="--auto-settle"),
        Opt(dest="no_settle", kind="flag", flag="--no-settle"),
        Opt(dest="no_context_pack", kind="flag", flag="--no-context-pack"),
        Opt(dest="no_plan", kind="flag", flag="--no-plan"),
        Opt(dest="quiet", kind="flag", flag="--quiet"),
        Opt(dest="verbose", kind="flag", flag="--verbose"),
        Opt(dest="verbose", kind="flag", flag="--no-verbose", negates=True),
        Opt(dest="no_watchdog", kind="flag", flag="--no-watchdog"),
        Opt(dest="fresh_adopt", kind="flag", flag="--fresh-adopt"),
        Opt(dest="json", kind="flag", flag="--json"),
        Opt(dest="speculative_review", kind="flag", flag="--speculative-review"),
        Opt(dest="optimize_after", kind="flag", flag="--optimize-after"),
        Opt(dest="optimize_apply", kind="flag", flag="--optimize-apply"),
        Opt(dest="best_of", kind="choice", tier=MAIN, flag="--best-of",
            choices=("1", "2", "3", "4")),
        Opt(dest="max_attempts", kind="value", flag="--max-attempts"),
        Opt(dest="review_rounds", kind="value", tier=MAIN, flag="--review-rounds"),
        Opt(dest="review_panel", kind="value", tier=MAIN, flag="--review-panel"),
        Opt(dest="review_report", kind="value", tier=MAIN, flag="--review-report"),
        Opt(dest="gate_timeout", kind="value", flag="--gate-timeout"),
        Opt(dest="worker_budget", kind="value", flag="--worker-budget"),
        Opt(dest="max_changed_paths", kind="value", flag="--max-changed-paths"),
        Opt(dest="allowed_prefix", kind="list", flag="--allowed-prefix"),
    ),
    "run2": (
        Opt(dest="feature_a", kind="positional", tier=MAIN),
        Opt(dest="feature_b", kind="positional", tier=MAIN),
        Opt(dest="repo", kind="value", flag="--repo"),
        Opt(dest="test_cmd", kind="value", flag="--test-cmd"),
        Opt(dest="provider", kind="choice", tier=MAIN, flag="--provider",
            choices=("claude", "static")),
        Opt(dest="no_review", kind="flag", flag="--no-review"),
        Opt(dest="verbose", kind="flag", flag="--verbose"),
        Opt(dest="verbose", kind="flag", flag="--no-verbose", negates=True),
        Opt(dest="auto_settle", kind="flag", flag="--auto-settle"),
        Opt(dest="no_settle", kind="flag", flag="--no-settle"),
        Opt(dest="no_context_pack", kind="flag", flag="--no-context-pack"),
        Opt(dest="no_plan", kind="flag", flag="--no-plan"),
        Opt(dest="optimize_after", kind="flag", flag="--optimize-after"),
        Opt(dest="optimize_apply", kind="flag", flag="--optimize-apply"),
        Opt(dest="speculative_review", kind="flag", flag="--speculative-review"),
        Opt(dest="max_attempts", kind="value", flag="--max-attempts"),
        Opt(dest="max_repairs", kind="value", flag="--max-repairs"),
        Opt(dest="gate_timeout", kind="value", flag="--gate-timeout"),
        Opt(dest="worker_budget", kind="value", flag="--worker-budget"),
        Opt(dest="max_changed_paths", kind="value", flag="--max-changed-paths"),
        Opt(dest="allowed_prefix", kind="list", flag="--allowed-prefix"),
    ),
    "runN": (
        Opt(dest="features", kind="positional", tier=MAIN, nargs="+"),
        Opt(dest="repo", kind="value", flag="--repo"),
        Opt(dest="test_cmd", kind="value", flag="--test-cmd"),
        Opt(dest="provider", kind="choice", tier=MAIN, flag="--provider",
            choices=("claude", "static")),
        Opt(dest="max_workers", kind="value", tier=MAIN, flag="--max-workers"),
        Opt(dest="no_review", kind="flag", flag="--no-review"),
        Opt(dest="max_attempts", kind="value", flag="--max-attempts"),
        Opt(dest="gate_timeout", kind="value", flag="--gate-timeout"),
        Opt(dest="worker_budget", kind="value", flag="--worker-budget"),
        Opt(dest="max_changed_paths", kind="value", flag="--max-changed-paths"),
        Opt(dest="allowed_prefix", kind="list", flag="--allowed-prefix"),
        Opt(dest="no_context_pack", kind="flag", flag="--no-context-pack"),
        Opt(dest="no_plan", kind="flag", flag="--no-plan"),
        Opt(dest="fresh_adopt", kind="flag", flag="--fresh-adopt"),
        Opt(dest="verbose", kind="flag", flag="--verbose"),
        Opt(dest="verbose", kind="flag", flag="--no-verbose", negates=True),
    ),
    "settle": (
        Opt(dest="run_ref", kind="positional", tier=MAIN),
        Opt(dest="repo", kind="value", flag="--repo"),
        Opt(dest="reject", kind="flag", tier=MAIN, flag="--reject"),
    ),
    "settle-par": (
        Opt(dest="proposal_id", kind="positional", tier=MAIN),
        Opt(dest="repo", kind="value", flag="--repo"),
        Opt(dest="reject", kind="flag", tier=MAIN, flag="--reject"),
    ),
    "init": (
        Opt(dest="repo", kind="value", flag="--repo"),
        Opt(dest="test_cmd", kind="value", tier=MAIN, flag="--test-cmd"),
        Opt(dest="no_gitignore", kind="flag", flag="--no-gitignore"),
        Opt(dest="review_panel", kind="value", tier=MAIN, flag="--review-panel"),
    ),
    "optimize": (
        Opt(dest="fix_n", kind="value", flag="--fix-n"),
        Opt(dest="guard_n", kind="value", flag="--guard-n"),
        Opt(dest="model", kind="value", flag="--model"),
        Opt(dest="worker_budget", kind="value", flag="--worker-budget"),
        Opt(dest="apply", kind="flag", tier=MAIN, flag="--apply"),
    ),
    "status": (
        Opt(dest="repo", kind="value", flag="--repo"),
        Opt(dest="limit", kind="value", flag="--limit"),
        Opt(dest="json", kind="flag", flag="--json"),
    ),
    "trace": (
        Opt(dest="run_id", kind="positional", tier=MAIN, nargs="?"),
        Opt(dest="full", kind="flag", tier=MAIN, flag="--full"),
        Opt(dest="json", kind="flag", flag="--json"),
    ),
    "update": (
        Opt(dest="force", kind="flag", flag="--force"),
    ),
}


def _is_unset(value: object) -> bool:
    """True when a value contributes nothing: absent, None, False, "" or []."""
    return value is None or value is False or value == "" or value == []


def build_argv(command: str, values: dict[str, object]) -> list[str]:
    """Assemble the argv for one command from the chosen values.

    A dest that is absent, None, False, "" or [] contributes nothing, so the
    parser's own default applies — the menu never has to know what that
    default is, and can never drift from it.

    `verbose` is the one dest with two entries in the table: `--verbose`
    and `--no-verbose` (`negates=True`) share it, mirroring argparse's
    store_true/store_false pair. Each entry fires only for the value it
    actually sets — the negating entry on False, the other on True —
    otherwise both would fire together on a shared True/False and silently
    flip the parsed result.

    Positionals are collected separately from options and joined at the
    end, because free text starting with `-` (a plausible feature request
    like "--dry-run support") would otherwise be read as a flag by
    argparse. A `--` separator is inserted before the positionals, but
    ONLY when one of them actually starts with `-` — the common case keeps
    today's positional-then-flags shape with no separator clutter.
    """
    table = OPTIONS[command]
    positionals: list[str] = []
    for opt in (o for o in table if o.kind == "positional"):
        value = values.get(opt.dest)
        if _is_unset(value):
            continue
        if isinstance(value, list):
            positionals.extend(str(v) for v in value)
        else:
            positionals.append(str(value))
    options: list[str] = []
    for opt in (o for o in table if o.kind != "positional"):
        value = values.get(opt.dest)
        if opt.kind == "flag":
            fires = value is False if opt.negates else value is True
            if fires:
                options.append(opt.flag)
            continue
        if _is_unset(value):
            continue
        if opt.kind == "list":
            for item in value:  # type: ignore[union-attr]
                options.extend([opt.flag, str(item)])
        else:
            options.extend([opt.flag, str(value)])
    argv: list[str] = [command]
    if any(part.startswith("-") for part in positionals):
        argv.extend(options)
        argv.append("--")
        argv.extend(positionals)
    else:
        argv.extend(positionals)
        argv.extend(options)
    return argv


def _read(prompt: str) -> str | None:
    """One line from the user. None means quit — EOF (a closed or piped
    stdin) and Ctrl-C both land here, and neither may raise past the menu."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def ask_choice(prompt: str, n: int) -> int | None:
    """A 1-based choice among n items. None = quit. Anything else re-asks.

    n < 1 has no valid answer and no way to reach it by re-prompting, so it
    quits immediately rather than looping forever.
    """
    if n < 1:
        return None
    while True:
        answer = _read(f"{prompt} [1-{n}, q quits]: ")
        if answer is None or answer.lower() == "q":
            return None
        # str.isdigit() accepts Unicode digits (e.g. '²') that int() then
        # rejects with ValueError — convert inside try/except, don't gate on
        # isdigit(), so garbage input re-asks instead of raising past here.
        try:
            n_answer = int(answer)
        except ValueError:
            continue
        if 1 <= n_answer <= n:
            return n_answer


def ask_text(prompt: str, default: str = "") -> str | None:
    answer = _read(prompt)
    if answer is None:
        return None
    return answer or default


def ask_value(opt: Opt, current: object) -> object | None:
    """One setting's value, honoring opt.kind. None = quit.

    The `flag` branch reads and writes in the ROW's own terms, not the
    shared dest's raw boolean: `on` is whatever makes this row's label
    true right now — `current is False` for the negating half of a pair
    like `--no-verbose`, `bool(current)` for every other flag — and
    toggling flips `on` before converting back to the dest's raw value.
    Without this, a negating row would display and toggle the dest's raw
    boolean directly: `--no-verbose` at its default (dest is None, i.e.
    falsy) would read "currently False" (as if off) and toggling it would
    set the dest True — producing `--verbose`, the opposite of the row
    picked. See OPTIONS' `negates` docstring.
    """
    if opt.kind == "flag":
        on = (current is False) if opt.negates else bool(current)
        answer = _read(f"{opt.flag}: currently {on} — [enter] toggles, q quits: ")
        if answer is None or answer.lower() == "q":
            return None
        new_on = not on
        return (not new_on) if opt.negates else new_on
    if opt.kind == "choice":
        for i, choice in enumerate(opt.choices, 1):
            print(f"    {i}) {choice}")
        picked = ask_choice(f"  {opt.flag}", len(opt.choices))
        return None if picked is None else opt.choices[picked - 1]
    # The prompt spells out both non-edit paths: bare Enter keeps the current
    # value (see _edit_loop's blank-input guard), "-" clears it back to
    # unset. Neither is discoverable without being shown.
    answer = _read(
        f"  {opt.flag} [{current if current not in (None, '', []) else 'unset'}, "
        "enter keeps, - clears]: "
    )
    if answer is None:
        return None
    if opt.kind == "list":
        return [part.strip() for part in answer.split(",") if part.strip()]
    return answer


def _decide_counts() -> dict[str, str]:
    """How much is waiting on the `decide` commands, for the first screen.

    Only `settle-par` can be counted exactly: a staged proposal is a directory
    under PROPOSALS_DIR and settling removes it. A `settle` proposal lives in
    the substrate, and whether one is still unconsumed is a question only the
    substrate can answer — which this module is deliberately not allowed to
    ask. So `settle` reports recent runs that RETAINED a proposal and says
    "recent", not "pending": claiming a precise pending count we cannot
    actually compute would be the kind of confident-and-wrong number a menu
    should never print.

    Best-effort throughout — a first screen must never fail to draw because a
    count could not be taken.
    """
    counts: dict[str, str] = {}
    try:
        from . import config
        from .staging import PROPOSALS_DIR

        repo_root = config.find_repo_root()
        if repo_root is not None and (repo_root / PROPOSALS_DIR).is_dir():
            staged = [p for p in (repo_root / PROPOSALS_DIR).iterdir() if p.is_dir()]
            if staged:
                counts["settle-par"] = f"{len(staged)} staged"
    except Exception:
        pass
    try:
        from .status import runs_status

        retained = [
            r for r in runs_status(limit=20)
            if r.get("state") == "succeeded" and r.get("final_run_ref")
        ]
        if retained:
            counts["settle"] = f"{len(retained)} recent"
    except Exception:
        pass
    return counts


def _pick_command() -> str | None:
    """First screen: the subcommands, grouped."""
    print("\nShepherd — supervised AI development\n")
    counts = _decide_counts()
    in_workspace = _repo_root() is not None
    ordered: list[str] = []
    for heading, names in GROUPS:
        print(f"  {heading}")
        for name in names:
            ordered.append(name)
            note = counts.get(name, "")
            if not note and not in_workspace and name in ("run", "run2", "runN"):
                # `init` is right there in the same menu, so say what is
                # missing rather than hiding the entries or failing later.
                note = "no workspace here — run init first"
            suffix = f"  ({note})" if note else ""
            print(f"    {len(ordered)}) {name}{suffix}")
        print()
    picked = ask_choice("choose", len(ordered))
    return None if picked is None else ordered[picked - 1]


def _repo_root():
    """The enclosing Shepherd workspace, or None. Never raises."""
    try:
        from . import config

        return config.find_repo_root()
    except Exception:
        return None


#: How `config.resolve_test_cmd`'s `source` reads on screen. Its "config"
#: means "saved in this repo's .shepherd-dev.json", which "saved" says more
#: plainly to someone looking at a menu.
_TEST_CMD_ORIGIN = {"config": "saved", "detected": "detected", "native": "native"}


def _prefill(command: str) -> tuple[dict[str, object], dict[str, str], dict[str, str]]:
    """What the repo already knows, and where each piece came from.

    Read-only, always: a one-off menu choice must not silently become the
    repo's default.

    Returns three mappings rather than folding them together, because they
    mean different things:

    - `values` — goes into argv. Only the repo's SAVED config lands here, as
      before; the flag really is being set on the user's behalf.
    - `origins` — how to label a value on screen ("from repo"); absent means
      the user chose it.
    - `hints`   — display only, never emitted. What a command would resolve
      for itself if the flag is left alone: the enclosing workspace for
      `--repo`, the resolved gate for `--test-cmd`. Emitting these would put
      a machine-specific absolute path into the equivalent command the menu
      prints for pasting, and would state as a choice something the user
      never made.

    `build_argv` still takes the plain dest->value mapping it was reviewed
    against.
    """
    values: dict[str, object] = {}
    origins: dict[str, str] = {}
    hints: dict[str, str] = {}
    repo_root = _repo_root()
    if repo_root is None:
        return values, origins, hints

    from . import config

    saved = config.load_config(repo_root)
    for opt in OPTIONS[command]:
        if opt.dest in saved:
            values[opt.dest] = saved[opt.dest]
            origins[opt.dest] = "from repo"

    by_dest = {o.dest for o in OPTIONS[command]}
    if "repo" in by_dest:
        hints["repo"] = f"{repo_root}  [detected]"
    if "test_cmd" in by_dest and "test_cmd" not in values:
        try:
            cmd, source, _ = config.resolve_test_cmd(repo_root, None)
        except Exception:
            cmd, source = None, "none"
        if cmd:
            hints["test_cmd"] = f"{cmd}  [{_TEST_CMD_ORIGIN.get(source, source)}]"
    return values, origins, hints


def _summary(
    command: str,
    values: dict[str, object],
    tier: str,
    origins: dict[str, str] | None = None,
    hints: dict[str, str] | None = None,
) -> None:
    origins = origins or {}
    hints = hints or {}
    shown = [o for o in OPTIONS[command] if o.tier == tier and o.kind != "positional"]
    for i, opt in enumerate(shown, 1):
        value = values.get(opt.dest)
        if value in (None, "", []):
            hint = hints.get(opt.dest)
            print(f"    {i}) {opt.flag:<22} {hint if hint else '(default)'}")
            continue
        origin = origins.get(opt.dest, "chosen")
        print(f"    {i}) {opt.flag:<22} {value}  [{origin}]")


def _edit_loop(
    command: str,
    values: dict[str, object],
    origins: dict[str, str] | None = None,
    hints: dict[str, str] | None = None,
) -> bool:
    """Second and third screens. False means the user quit."""
    origins = {} if origins is None else origins
    hints = {} if hints is None else hints
    tier = MAIN
    while True:
        print(f"\n{command} — {tier} settings\n")
        _summary(command, values, tier, origins, hints)
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
            # ask_value's free-text branch returns "" (kind="value") or []
            # (kind="list") on a bare Enter rather than echoing the current
            # value back the way ask_text does. Left as-is, a user who opens
            # a field just to look at it and hits Enter would blank it.
            # Treat that bare-Enter result as "leave unchanged" instead:
            # choice answers can't be "" here (ask_choice re-prompts until a
            # valid pick or quit) and flag answers are always a bool, so this
            # only ever intercepts the no-op case, never a real edit.
            if opt.kind in ("value", "list") and new in ("", []):
                continue
            # "-" (advertised in ask_value's prompt) is the deliberate
            # opposite of a bare Enter: it explicitly clears a field back to
            # unset — including one _prefill populated from the repo's saved
            # config — so build_argv omits the flag and the parser's own
            # default applies. Checked against every value/list dest in
            # OPTIONS: none is a shell command, path, model name or number
            # that is legitimately the bare string "-", so it collides with
            # no real value.
            # Whatever the user does below, the value stops being the repo's
            # or a detected one — drop the recorded origin so the summary says
            # "chosen" rather than keeping a now-false "from repo".
            origins.pop(opt.dest, None)
            if opt.kind == "value" and new == "-":
                values[opt.dest] = ""
                continue
            if opt.kind == "list" and new == ["-"]:
                values[opt.dest] = []
                continue
            values[opt.dest] = new


def run_menu(argv_out: list[str]) -> int | None:
    """Fill argv_out with the argv the user assembled and return 0.
    Return None when the user quit — nothing should run."""
    command = _pick_command()
    if command is None:
        return None
    values, origins, hints = _prefill(command)
    for opt in (o for o in OPTIONS[command] if o.kind == "positional"):
        answer = ask_text(f"\n  {opt.dest}: ")
        if answer is None:  # quit — EOF or Ctrl-C, never run on that
            return None
        if not answer:
            if opt.nargs == "?":
                # An optional positional (trace's run_id, nargs="?",
                # default="last") has a sensible default of its own — a
                # blank answer means "use it," not "cancel." Leaving
                # values[opt.dest] unset is enough: build_argv already
                # omits any unset positional, so the parser's own default
                # applies.
                continue
            return None  # a required field, blank cancels
        if opt.nargs == "+":
            # Strip each part and drop empties, matching ask_value's list
            # branch (kind="list") — a bare split(",") lets a stray comma
            # ("a,,b") or trailing comma ("a, ") through as an EMPTY feature
            # request, which reaches the worker. If nothing usable survives
            # ("," or " , " alone), that is an empty required field by the
            # same rule as a bare Enter above: cancel rather than run empty.
            features = [part.strip() for part in answer.split(",") if part.strip()]
            if not features:
                return None
            values[opt.dest] = features
        else:
            values[opt.dest] = answer
    if not _edit_loop(command, values, origins, hints):
        return None
    argv_out.extend(build_argv(command, values))
    return 0
