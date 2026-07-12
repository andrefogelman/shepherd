---
name: shepherd-dev
description: "Use when the user asks for supervised/sandboxed AI development — phrases like 'develop this feature with shepherd', 'supervised worker', 'run2 / parallel workers', 'settle a proposal', or wanting a feature implemented with a test gate and human approval before files change. Don't use for ordinary in-session coding you should do yourself (just edit the code), and don't use for installing the underlying shepherd-ai framework runtime (that is a dependency handled by the bootstrap)."
---

# shepherd-dev — supervised AI development via Shepherd

Drives the `shepherd-dev` CLI: a sandboxed Claude worker implements a feature in an
isolated Shepherd run; a deterministic supervisor applies a changeset policy, gates the
proposal on the repo's own test suite, retries with structured guidance, runs a skeptical
reviewer, and retains everything for **human-only settlement**. Nothing touches the
user's files until they accept.

## Preflight (every use)

1. `command -v shepherd-dev` — if missing, run the plugin bootstrap:
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.sh"` and show the user its output.
2. Target repo must be Shepherd-initialized once: `.vcscore/` exists, else run
   `shepherd-dev init --repo <path>` (it also gitignores the Shepherd state).
3. A test command must exist for the gate (`npm test`, `pytest -q`, `mix test`...).
   If the repo has no runnable suite, tell the user the gate needs one — do not fake it.

## Develop one feature

```bash
shepherd-dev run "<feature in natural language>" --repo <path> --test-cmd "<suite cmd>"
```

Options: `--mode tests` (only write/update tests), `--no-review`, `--max-attempts N`
(default 3), `--worker-budget SECONDS` (default 900), `--allowed-prefix src/`
(repeatable scope confinement), `--provider static` (offline dry-run of the machinery).

## Two coordinated parallel workers

```bash
shepherd-dev run2 "<feature A>" "<feature B>" --repo <path> --test-cmd "<suite cmd>"
```

Leader/follower conflict handoff, combined test gate with repair rounds
(`--max-repairs`, default 2), combined review, proposal staged under
`.shepherd-proposals/<id>/`.

## Settlement — ALWAYS a human decision

`run`/`run2` prompt the user inline at the end (accept / reject / diff) when stdin is a
TTY. When you drive the CLI yourself (stdin is not a TTY), the prompt is skipped and the
proposal stays retained — so YOU must report the run summary (attempts, gate, review
verdict) and the retained ref to the user, then WAIT for their decision. Never settle on
your own initiative; pass `--no-settle` if you want to be explicit about deferring.

```bash
shepherd-dev settle <run-ref> --repo <path>            # accept: writes files
shepherd-dev settle <run-ref> --repo <path> --reject   # discard
shepherd-dev settle-par <proposal-id> --repo <path> [--reject]   # run2 proposals
```

After an accepted settle, the files are in the working tree — the git commit also
belongs to the user (ask before committing).

## Known constraints (shepherd-ai 0.3.0, verified)

- Each `run` recreates the repo's `.vcscore` (stateless substrate; the git worktree is
  the source of truth). It refuses to run while an unconsumed proposal is pending —
  settle first.
- Worker deletions of files cannot be expressed; proposals only add/modify.
- Requires per machine: Python 3.11+, git, authenticated `claude` CLI, macOS
  (Seatbelt) or Linux with Landlock (kernel 5.13+).
- Long features: raise `--worker-budget`; each attempt is wall-clock bounded.
