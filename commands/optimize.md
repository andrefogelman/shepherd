---
description: CRO-lite — mine run history, propose a worker-prompt edit, validate by replay
argument-hint: [--apply] [--fix-n N] [--guard-n N] [--model M]
---

Improve the shepherd-dev worker prompts from the accumulated run history: $ARGUMENTS.

1. Run `shepherd-dev optimize [--apply] [--fix-n N] [--guard-n N] [--model M]`.
2. What it does: mines the history for failure modes, asks a meta-optimizer (Claude,
   default Opus) for ONE prompt edit, and validates it by REAL replay — each historical
   case re-run at its original commit with the candidate prompt injected. Accepts only if
   the fix set improves and the guard set does not regress.
3. Default is a dry-run; `--apply` persists the edit to `~/.shepherd-dev/prompts-overrides.json`.
4. Report: the proposed edit, fix/guard before→after, and the accept/reject decision.
   Note: costs real tokens (whole-case replay) and only helps once history has accumulated.

Flags:
- `--apply` — persist the edit if it passes (default: dry-run).
- `--fix-n N` — past failures to replay, must improve (default 3).
- `--guard-n N` — past passes to replay, must not regress (default 3).
- `--model M` — meta-optimizer model (default `claude-opus-4-8`).

Automatic triggering also exists: `run --optimize-after`, or `auto_optimize`
(`{"every_failures": N, "apply": bool}`) in `.shepherd-dev.json` / `~/.shepherd-dev/config.json`.
