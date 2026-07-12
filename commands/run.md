---
description: Develop a feature with a supervised sandboxed worker (test gate + review, human settlement)
argument-hint: "<feature>" [--repo P] [--test-cmd "…"] [--best-of K] [--mode tests] [--auto-settle] [--no-review] [--allowed-prefix P] [--max-attempts N] [--worker-budget S]
---

Use the shepherd-dev skill (invoke it first if not loaded) to run ONE supervised
development cycle for the request in $ARGUMENTS.

Steps:
1. Preflight per the skill. `--repo` defaults to the enclosing Shepherd workspace and
   `--test-cmd` to the saved `.shepherd-dev.json` / auto-detection / native gate — so
   usually only the feature is needed. Confirm the resolved repo + gate before running.
2. Run `shepherd-dev run "<feature>" ...` with any flags the user asked for (below).
3. Report the summary (attempts, gate result, review verdict) and the retained run ref.
4. WAIT for the user's settlement decision — never settle on your own unless `--auto-settle`.

Flags for `run`:
- `--repo <path>` — target repo (default: enclosing workspace).
- `--test-cmd "<cmd>"` — gate command (default: saved config → detection → native gate).
- `--best-of K` — K candidates (2–4) from the same state; deterministic ranking stages the winner.
- `--mode tests` — worker only writes/updates tests, not production code (default: `feature`).
- `--auto-settle` — on gate PASS + review APPROVED, settle + commit on an isolated `shepherd/<slug>` branch (never pushes). Incompatible with `--no-review`/`--provider static`.
- `--no-settle` — do not prompt to accept/reject; leave the proposal retained.
- `--no-review` — skip the reviewer (faster/cheaper).
- `--allowed-prefix <p>` — confine changes to a path prefix (repeatable).
- `--max-attempts N` — attempts before giving up (default 3).
- `--worker-budget S` — wall-clock seconds per attempt (default 900); raise for large features.
- `--provider static` — offline dry-run of the machinery, no LLM, no cost.
- `--optimize-after` — run `optimize` after this run (dry-run; `--optimize-apply` persists a passing edit).
