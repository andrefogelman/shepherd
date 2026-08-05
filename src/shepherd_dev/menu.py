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
        Opt(dest="verbose", kind="flag", flag="--no-verbose"),
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
        Opt(dest="verbose", kind="flag", flag="--no-verbose"),
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
        Opt(dest="verbose", kind="flag", flag="--no-verbose"),
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
