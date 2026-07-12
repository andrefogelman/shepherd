---
description: Accept or reject a retained shepherd-dev proposal (writes files only on accept)
argument-hint: <run-ref | proposal-id> [--repo <path>] [--reject]
---

The user is settling a shepherd-dev proposal: $ARGUMENTS.

1. If the ref starts with `run-` use `shepherd-dev settle`, otherwise it is a run2
   proposal id — use `shepherd-dev settle-par`.
2. Before accepting, show the user what will change if they haven't seen it
   (`shepherd run changeset <run-ref>` for runs; the staged manifest for run2).
3. Execute the settlement the user asked for and report the written files.
4. The git commit belongs to the user — ask whether to commit.
