---
description: Develop two features with coordinated parallel workers (conflict handoff, combined gate + review)
argument-hint: "<A>" "<B>" [--repo P] [--test-cmd "…"] [--auto-settle] [--no-review] [--max-repairs N] [--max-attempts N] [--allowed-prefix P] [--worker-budget S]
---

Use the shepherd-dev skill (invoke it first if not loaded) to run TWO coordinated
parallel workers for the request in $ARGUMENTS.

Steps:
1. Preflight per the skill; `--repo` and `--test-cmd` are inferred like `run`. Confirm both.
2. Run `shepherd-dev run2 "<A>" "<B>" ...` with any flags the user asked for (below).
3. Report: per-worker verdicts, conflicts/handoff, combined gate (and repairs), review
   verdict, and the staged proposal id.
4. WAIT for the user's settlement decision (`settle-par`) — never settle unless `--auto-settle`.

Flags for `run2`:
- `--repo <path>` — target repo (default: enclosing workspace).
- `--test-cmd "<cmd>"` — combined gate (default: config → detection → native gate).
- `--auto-settle` — on combined gate PASS + review APPROVED, settle + commit on `shepherd/<slug>` (never pushes). Incompatible with `--no-review`/`--provider static`.
- `--no-settle` — do not prompt; leave the proposal staged.
- `--no-review` — skip the reviewer.
- `--max-repairs N` — repair rounds on the combined gate (default 2).
- `--max-attempts N` — attempts per worker (default 2).
- `--allowed-prefix <p>` — confine changes to a path prefix (repeatable).
- `--worker-budget S` — wall-clock seconds per attempt (default 900).
- `--provider static` — offline dry-run, no LLM.
- `--optimize-after` — run `optimize` after this run (dry-run; `--optimize-apply` persists a passing edit).
