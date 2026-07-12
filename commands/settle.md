---
description: Accept or reject a retained run proposal (writes files only on accept)
argument-hint: <run-ref> [--repo P] [--reject]
---

The user is settling a retained `run` proposal: $ARGUMENTS.

1. If the ref is a run2/best-of proposal id (not `run-…`), use `/shepherd-dev:settle-par` instead.
2. Before accepting, show what will change if they haven't seen it
   (`shepherd run changeset <run-ref>`).
3. Run `shepherd-dev settle <run-ref> [--repo <path>] [--reject]`.
   `--repo` defaults to the enclosing workspace. `--reject` discards instead of accepting.
4. Report the written files. The git commit belongs to the user — ask whether to commit.
