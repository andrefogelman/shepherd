---
description: Accept or reject a staged run2 / best-of proposal (writes files only on accept)
argument-hint: <proposal-id> [--repo P] [--reject]
---

The user is settling a staged parallel/best-of proposal: $ARGUMENTS.

1. Before accepting, show what will change if they haven't seen it (the staged manifest
   under `.shepherd-proposals/<id>/`).
2. Run `shepherd-dev settle-par <proposal-id> [--repo <path>] [--reject]`.
   `--repo` defaults to the enclosing workspace. `--reject` discards instead of accepting.
3. Report the written files. The git commit belongs to the user — ask whether to commit.
