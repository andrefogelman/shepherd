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
        Opt(dest="features", kind="positional", tier=MAIN),
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
        Opt(dest="run_id", kind="positional", tier=MAIN),
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
    """
    argv: list[str] = [command]
    table = OPTIONS[command]
    for opt in (o for o in table if o.kind == "positional"):
        value = values.get(opt.dest)
        if _is_unset(value):
            continue
        if isinstance(value, list):
            argv.extend(str(v) for v in value)
        else:
            argv.append(str(value))
    for opt in (o for o in table if o.kind != "positional"):
        value = values.get(opt.dest)
        if opt.kind == "flag":
            fires = value is False if opt.negates else value is True
            if fires:
                argv.append(opt.flag)
            continue
        if _is_unset(value):
            continue
        if opt.kind == "list":
            for item in value:  # type: ignore[union-attr]
                argv.extend([opt.flag, str(item)])
        else:
            argv.extend([opt.flag, str(value)])
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

    The `flag` branch is a plain toggle (`not bool(current)`) regardless of
    `opt.negates` — polarity is build_argv's concern, not this one's. See
    OPTIONS' `negates` docstring.
    """
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
