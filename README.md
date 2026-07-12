# shepherd-dev

Supervised AI development on top of [Shepherd](https://github.com/shepherd-agents/shepherd)
(arXiv [2605.10913](https://arxiv.org/abs/2605.10913)): sandboxed Claude workers implement
features; a deterministic supervisor applies a changeset policy, gates every proposal on the
repo's own test suite, retries with structured guidance, runs a skeptical reviewer, and holds
everything for **human-only settlement** — nothing touches your files until you accept it.

## Install (any machine)

```bash
uv tool install git+ssh://git@github.com/andrefogelman/shepherd.git
# or: pipx install git+ssh://git@github.com/andrefogelman/shepherd.git
```

Requirements per machine: Python 3.11+, `git`, the `claude` CLI installed and authenticated
(subscription or API key), and a jail-capable OS (macOS Seatbelt / Linux with Landlock,
kernel 5.13+). Windows: use WSL.

One-time per target repo (gitignores the Shepherd state and saves the detected
test command, so later `run`s need no flags):

```bash
cd ~/projects/my-repo && shepherd-dev init
```

## Use

```bash
# from inside the repo: --repo and --test-cmd are inferred
# on an interactive terminal it then prompts: accept (a) / reject (r) / diff (d)
cd ~/projects/my-repo
shepherd-dev run "add CPF validation to signup"

# in a pipe/CI, or with --no-settle, the proposal stays retained; settle later:
shepherd-dev settle <run-ref> [--reject]

# two coordinated parallel workers (conflict handoff + combined gate + review)
shepherd-dev run2 "feature A" "feature B"
shepherd-dev settle-par <proposal-id> [--reject]
```

Useful flags: `--mode tests` (only write tests), `--no-review`, `--provider static` (offline
dry-run of the machinery), `--allowed-prefix src/` (scope confinement), `--max-attempts`,
`--worker-budget` (wall-clock seconds per attempt), `--max-repairs` (run2).

## Claude Code plugin

This repo is also a Claude Code plugin marketplace. On any machine with Claude Code:

```
/plugin marketplace add andrefogelman/shepherd
/plugin install shepherd-dev@shepherd
```

The plugin ships a skill (teaches Claude when/how to drive `shepherd-dev`), slash commands
(`/shepherd-dev:run`, `/shepherd-dev:run2`, `/shepherd-dev:settle`) and a bootstrap script
that installs the CLI if missing.

## Design docs

See `docs/2026-07-11-dev-layer-design.md` — including the empirically-verified constraints of
shepherd-ai 0.3.0 (stateless per-invocation substrate, custody-based reviewer isolation,
worktree as source of truth) in the F2/F3 addenda.

Based on the article https://arxiv.org/html/2605.10913
Shepherd: Enabling Programmable Meta-Agents via Reversible Agentic Execution Traces
