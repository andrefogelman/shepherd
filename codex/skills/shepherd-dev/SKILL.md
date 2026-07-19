---
name: shepherd-dev
description: "Use when the user asks for supervised/sandboxed AI development — phrases like 'develop this feature with shepherd', 'supervised worker', 'run2 / parallel workers', 'settle a proposal', or wanting a feature implemented with a test gate and human approval before files change. Don't use for ordinary coding you should do yourself (just edit the code)."
---

# shepherd-dev — supervised AI development via Shepherd

Drives the `shepherd-dev` CLI in the terminal: a sandboxed worker implements a feature in
an isolated Shepherd run; a deterministic supervisor applies a changeset policy, gates the
proposal on the repo's own test suite, retries with structured guidance, runs a skeptical
reviewer, and retains everything for **human-only settlement**. Nothing touches the user's
files until they accept.

This SKILL.md is the open agentskills.io standard — the same skill works in Codex CLI,
Claude Code, Cursor, and other agents. Drive the CLI; do not edit the files yourself.

## Preflight (every use)

1. `command -v shepherd-dev` — if missing, install it once:
   `uv tool install git+https://github.com/andrefogelman/shepherd.git`
   (needs Python 3.11+, git, and an authenticated `claude` CLI — the worker is a headless
   Claude Code session; the host agent's own model does NOT power the worker).
2. Target repo must be Shepherd-initialized once: `.vcscore/` exists, else run
   `shepherd-dev init --repo <path>` (gitignores the state AND saves the detected test
   command to `.shepherd-dev.json`).
3. A test command resolves automatically. Precedence: `--test-cmd` > saved
   `.shepherd-dev.json` > auto-detection > native zero-dep gate (node --test /
   python unittest / mix test / cargo test) with the worker writing its own tests. Usually
   you pass ONLY the feature.

## Develop one feature

```bash
# --repo defaults to the enclosing repo; --test-cmd to the saved/detected one
shepherd-dev run "<feature in natural language>"
# override either when needed:
shepherd-dev run "<feature>" --repo <path> --test-cmd "<suite cmd>"
```

Options: `--mode tests` (only write/update tests), `--no-review`, `--max-attempts N`
(default 3), `--worker-budget SECONDS` (default 900), `--allowed-prefix src/` (repeatable
scope confinement), `--best-of K` (2-4 candidates), `--provider static` (offline dry-run),
`--provider codex` (worker via this very Codex CLI — no Claude subprocess; adds a real
LLM review of the proposal, so `--auto-settle` works), `--provider grok` (Grok CLI worker).

## Two coordinated parallel workers

```bash
shepherd-dev run2 "<feature A>" "<feature B>"
```

Leader/follower conflict handoff, combined test gate with repair rounds
(`--max-repairs`, default 2), combined review, proposal staged under
`.shepherd-proposals/<id>/`.

## Settlement — ALWAYS a human decision

`run`/`run2` prompt inline at the end (accept / reject / diff) when stdin is a TTY. When
YOU drive the CLI (stdin is not a TTY), the prompt is skipped and the proposal stays
retained — so report the run summary (attempts, gate, review verdict) and the retained ref
to the user, then WAIT for their decision. Never settle on your own initiative; pass
`--no-settle` to be explicit about deferring.

```bash
shepherd-dev settle <run-ref> --repo <path>            # accept: writes files
shepherd-dev settle <run-ref> --repo <path> --reject   # discard
shepherd-dev settle-par <proposal-id> --repo <path> [--reject]   # run2 proposals
```

After an accepted settle, the files are in the working tree — the git commit also belongs
to the user (ask before committing).

## Known constraints (verified)

- Each `run` recreates the repo's `.vcscore` (stateless substrate; the git worktree is the
  source of truth). It refuses to run while an unconsumed proposal is pending — settle first.
- Worker deletions of files cannot be expressed; proposals only add/modify.
- Requires per machine: Python 3.11+, git, authenticated `claude` CLI, macOS (Seatbelt) or
  Linux with Landlock (kernel 5.13+).
- Long features: raise `--worker-budget`; each attempt is wall-clock bounded (hard-killed
  at the budget).
