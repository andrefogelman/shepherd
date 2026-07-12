---
description: Initialize a repo as a Shepherd workspace (one-time) — gitignore + test-cmd detection
argument-hint: [--repo P] [--test-cmd "…"] [--no-gitignore]
---

Initialize the target repo for shepherd-dev (run once per repo): $ARGUMENTS.

1. Run `shepherd-dev init [--repo <path>] [--test-cmd "<cmd>"] [--no-gitignore]`.
   `--repo` defaults to the current directory.
2. What it does: initializes the Shepherd workspace, appends the local state to
   `.gitignore` (`.vcscore/`, `REVIEW.json`, `.shepherd-proposals/`), and saves the
   detected test command to `.shepherd-dev.json` so later `run`s need no `--test-cmd`.
3. For an Elixir project without ExUnit, it announces and generates the minimal
   `test/test_helper.exs` scaffold.
4. Report what it created/detected.

Flags:
- `--repo <path>` — repo to initialize (default: cwd).
- `--test-cmd "<cmd>"` — save this gate command explicitly (else auto-detect and save).
- `--no-gitignore` — don't touch `.gitignore`.
