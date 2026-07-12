---
description: Develop a feature with a supervised sandboxed worker (test gate + review, human settlement)
argument-hint: "<feature>" [--repo <path>] [--test-cmd "<suite cmd>"]
---

Use the shepherd-dev skill (invoke it first if not loaded) to run ONE supervised
development cycle for the request in $ARGUMENTS.

Steps:
1. Preflight per the skill (CLI installed, repo shepherd-initialized, test command known —
   --repo defaults to the enclosing Shepherd workspace and --test-cmd to the saved
   `.shepherd-dev.json` or auto-detection — so usually just the feature is needed.
   Confirm the resolved repo and test command with the user before running).
2. Run `shepherd-dev run ...` with the confirmed arguments.
3. Report the summary (attempts, gate result, review verdict) and the retained run ref.
4. WAIT for the user's settlement decision — never settle without it.
