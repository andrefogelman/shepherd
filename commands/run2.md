---
description: Develop two features with coordinated parallel workers (conflict handoff, combined gate + review)
argument-hint: "<feature A>" "<feature B>" [--repo <path>] [--test-cmd "<suite cmd>"]
---

Use the shepherd-dev skill (invoke it first if not loaded) to run TWO coordinated
parallel workers for the request in $ARGUMENTS.

Steps:
1. Preflight per the skill; confirm repo and test command with the user if not given.
2. Run `shepherd-dev run2 "<feature A>" "<feature B>" ...`.
3. Report: per-worker verdicts, conflicts/handoff, combined gate (and repairs), review
   verdict, and the staged proposal id.
4. WAIT for the user's settlement decision (`settle-par`) — never settle without it.
