# Working in this repository as an agent

Read by humans and by every coding agent that opens the repo, including
shepherd-dev's own worker when it runs against this checkout. The context pack
injects just under `INSTRUCTIONS_BUDGET` (4,000) characters of this file, the
rest of the budget going to a truncation marker (`src/shepherd_dev/contextpack.py`;
the budget is shared with `CLAUDE.md` and `.github/copilot-instructions.md`
when they exist), so what the worker needs comes first.
Where this file conflicts with a skill or a tool default, this file wins.

## Invariants

- **Generic solutions only.** shepherd-dev is a public, general-purpose tool.
  No host names, IPs, personal paths or machine nicknames in code, defaults,
  tests or docs. What is specific to a user lives in that user's config
  (`.shepherd-dev.json`, `~/.shepherd-dev/`). Validation uses toy repos and
  fixtures.
- **Settlement is human-only.** Nothing writes to the user's files until a
  person accepts. The MCP settle tools refuse without `confirm: true`. The
  only self-applying path is the explicit `--auto-settle`, which commits on an
  isolated `shepherd/<slug>` branch and never pushes.
- **Stdlib only** beyond `shepherd-ai`. No new third-party dependency.
- **Every new CLI flag is classified** in `src/shepherd_dev/menu.py`
  (`OPTIONS`) in the same change that adds it, or `tests/test_menu.py` fails.
  An append action never declares argparse `choices=`; validate values in code.
- **Review lens texts** live in `prompts.py` but stay out of `PROMPT_KEYS` and
  of `optimize.py`'s `EDITABLE_KEYS`: a lens catalogue is a taxonomy, not a
  prompt under optimization.
- **`tasks.py` carries an inlined copy** of the prompt registry (the substrate
  forbids a task source importing same-package modules). Keep both in sync.
- **A fixed defect is pinned by a test** and recorded in `docs/KNOWN_ISSUES.md`
  with its mechanism: what was, why, what the fix changed, which test pins it.
- **Changing a default changes every user.** New behaviour is opt-in unless the
  change is itself the fix; with the option off the existing code path stays
  byte-for-byte and the existing tests pass untouched.
- Read the neighbouring code before writing and write like it. Touch only what
  the task needs. Comments say why, not what.

## Commands

| what | command |
|---|---|
| suite | `env -u PYTHONPATH python -m unittest discover -s tests -v` |
| one module | `env -u PYTHONPATH python -m unittest tests.test_<name>` |
| syntax and undefined names | `ruff check --select E9,F821,F822,F823 src tests/test_io_perf.py scripts` (ruff is not a dependency; CI's `static` job installs `ruff==0.12.10`) |
| lockfile matches pyproject | `uv lock --check` |
| timing-sensitive modules ×10 (CI's `repeat` job) | `for i in $(seq 1 10); do env -u PYTHONPATH python -m unittest tests.test_diffcollect tests.test_perf tests.test_teepump tests.test_io_perf tests.test_events tests.test_gatestream; done` |
| type diagnostics vs baseline | `python scripts/check_type_delta.py <baseline-checkout> .` (needs `pyright`, which CI's `static` job installs at 1.1.403; baseline commit pinned in `.github/workflows/ci.yml`. On a machine where nothing may be installed, read the result from that job) |
| I/O benchmark | `python scripts/benchmark_io.py --source-root . --repeat 3` |

`PYTHONPATH` stays unset on purpose: the gate scrubs it, and a run that leaked
one would be testing something else. CI runs the suite on ubuntu and macOS with
Python 3.11, 3.13 and 3.14 because a defect once reproduced on one filesystem
only; a green suite on one laptop is not the suite.

## Language and style

New code, comments, commit messages, PR text and docs are in English; older
Portuguese material stays as it is. `docs/MANUAL.md` (Portuguese) is kept in
step with `docs/MANUAL.en.md`. A commit message says the defect observed and
the mechanism of the change, not the file list.

## Gate at the start of every task

```bash
git fetch --prune origin
git branch -r --no-merged origin/main    # older than 3 days: report it, do not resolve it
gh pr list                               # is another agent working here?
```

An open PR means `gh pr diff <n> --name-only`. A file in common with the task:
stop and ask. The result of the three commands goes into the first reply even
when it is "nothing". Run `gh pr list` again before the first push: a
concurrent PR can appear after the branch was created.

## The four steps

1. **Isolate.** Every task gets its own branch, born from `origin/main`
   (`git switch -c <type>/<slug> origin/main`; types `feat`, `fix`, `chore`,
   `docs`, `ci`). Never build on `main`, never share a branch with another
   agent. A harness-managed worktree (`.claude/worktrees/`, gitignored) keeps
   its assigned branch; bring it to the tip with `git merge --ff-only
   origin/main` before use. Worktrees do not share `.venv`.
2. **Build.** Per the invariants above. Extracting shared mechanics is a
   refactor: propose it, do not assume it.
3. **Prove.** The repo's checks plus evidence of execution. For a defect, the
   "before" is the first act: reproduce it and keep the proof before writing
   the fix. There is no UI here, so evidence is measured numbers, output
   pairs, a test that fails before and passes after, or a transcript excerpt.
   It goes to `.artifacts/<task>/` (gitignored) with an `assertions.md`
   listing each assertion as `passed`, `failed` or `untested` plus the reason.
   Evidence complements the checks; it never replaces them.
4. **Deliver.** Local review of the diff against `origin/main` before the
   first push and again before every later push; findings are fixed on the
   branch. Then push, open the PR, and iterate on it until the
   `pullfrog-approval` check is green with no unresolved thread. The PR body
   carries the proof from step 3: measured numbers, output pairs, the test
   that fails before and passes after. A file (log, capture) goes up only as
   a native GitHub attachment (`gh pr edit --attach`, `gh pr comment
   --attach`; `gh` ≥ 2.88), never through an outside host. Finish by
   reporting the PR URL and the state of its checks, and stop. The merge is
   the owner's, on explicit order.

Before committing or posting text written for people (commit message, PR
title and body, docs, comments), strip the AI tells from it. Leave prose the
task did not touch alone.

## Git

- `main` only receives squash merges of PRs; the ruleset enforces it. One
  order, one branch, one PR. One commit per fix inside the branch.
- The PR opens at the first push (`gh pr create --base main`), ready for
  review, not as a draft: the review bot skips drafts (`review.drafts=false`
  in the Pullfrog app's per-repo config, outside this repo; `npx pullfrog
  config list` shows it). A remote branch without an open PR is a violation.
- Update the branch with `git merge origin/main`. Never rebase after the first
  push; `--force` and `--force-with-lease` are forbidden everywhere.
- `git branch --show-current` before every push. `main` means do not push.
- Add files by explicit path, never `git add -A`. Never `--no-verify`.
- Lint, type check and the suite run before each commit. Each push to an open
  PR runs the CI matrix and a re-review: push once per iteration, not per
  commit.
- Which machine runs the suite is the owner's decision, asked once per session
  before the first run. Nothing is installed on a machine without the owner's
  say.

## Versions and releases

A version bump touches five files in one commit (`chore: X.Y.Z`):
`pyproject.toml`, `src/shepherd_dev/__init__.py`, `.claude-plugin/plugin.json`,
`kimi.plugin.json` and `uv.lock` (run `uv lock`; CI's `lock` job fails
otherwise). The bump reaches `main` by PR like anything else.

A bump without a release is a version nobody can see. After the merge, the
owner tags it (`git tag vX.Y.Z && git push origin vX.Y.Z`): the tag push runs
the suite and publishes the GitHub Release (`.github/workflows/release.yml`).
Notes written by hand before the tag push win over the generated ones; the
workflow leaves an existing release alone. `updatecheck.py` reads the version
from `main`, so an unbumped change is invisible to `shepherd-dev update`.

## What is not tested here

- Real providers (`claude`, `codex`, `grok` CLIs) cost tokens and need a login;
  the suite stubs them. `--provider static` is the offline dry run.
- Native jails (macOS Seatbelt, Linux Landlock) are exercised by provider runs,
  not by the suite.
- The performance and type-delta jobs compare against a pinned baseline
  commit; a regression there is a finding to fix, not a number to re-pin.
