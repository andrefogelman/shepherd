# shepherd-dev — user manual

A Claude worker implements a feature inside a sandbox. A deterministic
supervisor applies policy, runs the repo's own test suite as a gate, retries
with guidance, and passes the result through a skeptical reviewer. **Nothing
touches your files until you accept.**

*(Versão em português: [MANUAL.md](MANUAL.md).)*

## Mental model

```
Worker (implements) → Policy (guards) → Gate (tests) → Reviewer (audits) → YOU (settle)
```

A passing result stays **retained** under a reference (`run-…`). It only
becomes files in your worktree when you run `settle`. The git commit remains
yours.

## Two ways to use it

**1. Inside Claude Code (recommended).** Install the plugin once and talk to
Claude — it drives shepherd-dev under the hood and conducts everything in the
conversation, no terminal needed. See "Using inside Claude Code" below.

**2. CLI directly in the terminal.** The `shepherd-dev` binary on your PATH.
See the rest of this manual.

Both use the same tool; the plugin is just the conversational layer.

## Using inside Claude Code

Install once (marketplace + plugin from this repo):

```
/plugin marketplace add andrefogelman/shepherd
/plugin install shepherd-dev@shepherd
```

Restart Claude Code (plugins load at startup). Then three ways to trigger it,
all conducted in-chat:

- **Natural language** — "develop a CPF validator in repo X with shepherd".
  The `shepherd-dev` skill triggers on its own.
- **Slash commands**:
  - `/shepherd-dev:run "<feature>"` — develop one feature.
  - `/shepherd-dev:run2 "<A>" "<B>"` — two parallel workers.
  - `/shepherd-dev:settle <ref>` — accept/reject a proposal.

Claude runs `shepherd-dev`, shows the report (attempts, gate, reviewer
verdict) and the proposed diff, then **asks you in chat**: accept or reject.
Nothing touches your files until you answer. If the CLI is missing on the
machine, the plugin's bootstrap installs it on first use.

## Install (per machine)

```bash
uv tool install git+https://github.com/andrefogelman/shepherd.git
# or, with Claude Code (brings the skill + /shepherd-dev:* commands):
#   /plugin marketplace add andrefogelman/shepherd
#   /plugin install shepherd-dev@shepherd
```

Requirements: Python 3.11+, git, an authenticated `claude` CLI, macOS
(Seatbelt) or Linux with Landlock (kernel ≥ 5.13). Windows: WSL.

## Prepare a repo (once)

```bash
cd ~/projects/my-app
shepherd-dev init
```

One command: initializes the Shepherd workspace, appends the local state to
`.gitignore` (`.vcscore/`, `REVIEW.json`, `.shepherd-proposals/`) without
duplicating, **and detects the test command**, saving it to
`.shepherd-dev.json` (project metadata — commit it). Later `run`s then need
no `--test-cmd`.

If your stack isn't auto-detected, state it once: `shepherd-dev init
--test-cmd "…"`. With no test command at all there is no gate — the tool says
so instead of pretending. `--no-gitignore` skips the gitignore step.

## The basic loop

From inside the repo, the everyday command is just the feature. `--repo`
defaults to the repo enclosing the current directory; `--test-cmd` comes from
what `init` saved (or is auto-detected). When it finishes, on an interactive
terminal it **asks** what to do:

```bash
cd ~/projects/my-app
shepherd-dev run "add CPF validation to signup"
```

Gate precedence: explicit `--test-cmd` → saved in `.shepherd-dev.json` →
stack auto-detection → **native gate** (universal floor) → error. Override
whenever you want: `--test-cmd "…"`, `--repo <path>`.

**Repo without tests?** No problem. When there is no configured or detectable
suite (or the declared `npm test` can't run because `node_modules` is
missing), shepherd uses a dependency-free native runner — `node --test`
(with strip-types for `.ts` on Node ≥ 22.6) or `python3 -m unittest` — **and
instructs the worker to write the tests alongside the feature**. You write
only the intent; the tests come in the package. The native gate runs exactly
the test files the proposal adds; a proposal without tests fails loudly.

```
... report: attempts, gate, reviewer verdict ...

Accept (a), reject (r) or view the diff (d)? [a/r/d]:
```

- `a` — accepts, writes the files into the worktree (review and git-commit
  whenever you like).
- `r` — discards the proposal.
- `d` — shows the proposed diff and asks again.
- Empty Enter — leaves it retained; decide later with `settle`.

The prompt only appears on an interactive terminal. In a pipe/CI (stdin is
not a terminal), or with `--no-settle`, the proposal stays retained and you
settle whenever you want:

```bash
shepherd-dev settle run-abc123 --repo ~/projects/my-app            # accept & write
shepherd-dev settle run-abc123 --repo ~/projects/my-app --reject   # discard
```

## Commands

| Command | What it does |
|---|---|
| `run "feat" --repo P --test-cmd "…"` | One feature, one supervised worker. Retained for `settle`. |
| `run2 "A" "B" --repo P --test-cmd "…"` | Two features, two parallel workers; conflict handoff; combined gate; winner staged for `settle-par`. |
| `run … --best-of K` | K candidates (2–4) from the same state; deterministic ranking; the best is staged. |
| `settle <run-ref> --repo P [--reject]` | Settles a `run` proposal. |
| `settle-par <proposal-id> --repo P [--reject]` | Settles a staged `run2` / `--best-of` proposal. |
| `init --repo P` | Initializes the repo (once). |
| `optimize [--apply]` | Improves the worker prompts from run history, validated by replay. |

## Useful flags

| Flag | Applies to | What it does |
|---|---|---|
| `--test-cmd` | run · run2 | Gate command (the objective arbiter). |
| `--mode tests` | run | Worker only writes tests, not production code. |
| `--best-of K` | run | K parallel candidates (2–4). |
| `--auto-settle` | run · run2 | Accepts on its own if the gate passed and the reviewer approved; commits on an isolated branch. |
| `--no-settle` | run · run2 | No prompt at the end; leaves the proposal retained. |
| `--no-context-pack` | run · run2 | Turns off the context pack (worker explores the repo itself — more expensive). |
| `--no-review` | run · run2 | Skips the reviewer. Incompatible with `--auto-settle`. |
| `--allowed-prefix` | run · run2 | Confines changes to a prefix (repeatable). |
| `--max-attempts` | run · run2 | Attempts per worker (default 3). |
| `--worker-budget` | run · run2 | Seconds per attempt (default 900). |
| `--max-repairs` | run2 | Repair rounds on the combined gate (default 2). |
| `--provider static` | run · run2 | Offline dry-run without an LLM (zero cost). |
| `--optimize-after` | run · run2 | Runs `optimize` when the run finishes (`--optimize-apply` persists). |

## Best-of-N

The essence of the paper's Tree-RL, at inference time and without training.
K candidates from the same state, with different emphases (neutral, smallest
diff, robustness, codebase idioms). All go through the gate; the ones that
pass go to the reviewer. Deterministic ranking: gate passed → reviewer
approved → fewer issues → fewer files → smaller diff.

```bash
shepherd-dev run "refactor the date parser" \
  --repo ~/projects/my-app --test-cmd "pytest -q" --best-of 3
```

## Auto-apply

`--auto-settle` accepts automatically **only if** the gate passed **and** the
reviewer approved. Any unmet criterion leaves the proposal retained.

- Reviewer is mandatory (`--no-review` and `static` are refused).
- Commits on an isolated `shepherd/<slug>` branch — never your current branch.
- **Never pushes.** Reverting is trivial.
- Automatic decisions are marked in the history.

Recommended together with `--allowed-prefix` in autonomous mode.

## Optimize — CRO-lite

Application 2 of the paper, honest to shepherd-ai 0.3.0. Mines the history
for failure modes, asks a meta-optimizer (Claude, default Opus) for one
prompt edit, and validates it by real replay: each case re-run in a git
worktree pinned to the original commit, with the candidate prompt injected.
Accepts only if the fix set improves and the guard set does not regress.

```bash
shepherd-dev optimize            # dry-run
shepherd-dev optimize --apply    # persists the edit if it passes
```

Costs real tokens (no cheap replay in the public lane) — small sets by
default (3/3). Becomes useful once the history has accumulated real runs.

**Automatic (two layers):**

- **Per-run flag**: `shepherd-dev run … --optimize-after` triggers `optimize`
  when the run finishes (dry-run; add `--optimize-apply` to persist).
- **Config default with a threshold trigger** — in `.shepherd-dev.json`
  (per repo) or `~/.shepherd-dev/config.json` (global):

  ```json
  { "auto_optimize": { "every_failures": 5, "apply": false } }
  ```

  `run` only fires optimize once N gate failures accumulate since the last
  optimize (counter in the history; any optimize — manual or automatic —
  resets it). Cost stays controlled: nothing runs without new material. Repo
  config wins over global; with no config, the automatic layer is off.

## Where things live

| Location | Contents |
|---|---|
| `~/.shepherd-dev/history/` | Run history (JSONL). Feeds `optimize` and auditing. |
| `~/.shepherd-dev/prompts-overrides.json` | Accepted prompt edits. Delete a key to restore the default. |
| `~/.shepherd-dev/memory/` | Per-repo learned memory (curated facts). |
| `<repo>/.vcscore/` | Workspace state (recreated on each `run`). |
| `<repo>/.shepherd-proposals/` | Staged proposals from `run2` / `--best-of`. |

Redirect envs: `SHEPHERD_DEV_HISTORY_DIR`, `SHEPHERD_DEV_PROMPTS_OVERRIDES`,
`SHEPHERD_DEV_MEMORY_DIR`.

## Token consumption

### Who consumes

**Only the Claude calls.** Shepherd's orchestration (Python: fork, gate,
policy, ranking, settlement, context pack, memory) is **zero tokens** — it
runs locally. All the spend is in the `claude -p` sessions of the worker,
the reviewer and the optimizer.

Important: the provider is the **`claude` CLI of your Max subscription**,
not the pay-per-token API. So the "cost" is **Max quota consumption**, not
dollars. Each worker/reviewer is a headless Claude Code session (agentic —
reads files, edits, iterates) that counts against the quota like a normal
dev session of yours.

### Per command (in "Claude sessions")

| Command | Worker | Reviewer | Typical total |
|---|---|---|---|
| `run` (1 feature) | 1 per attempt (up to `--max-attempts`, def. 3) | 1 (only if the gate passes) | 1–3 workers + 1 review |
| `run2` | 2 parallel + handoff + repairs (`--max-repairs`) | 1 (of the combined diff) | ~2–5 + 1 |
| `run --best-of K` | K workers | up to K (one per passing candidate) | K + up to K |
| `optimize` | replay: 1 per case (fix-n + guard-n, def. 3+3 = 6) | — | 6 workers + 1 meta (Opus) |
| any `--provider static` | **0** | 0 | **free** (offline) |

### Context pack + memory: the native optimization

Each `run`/`run2`/`best-of` builds locally (zero cost, ~2s) a **context
pack**: repo tree + feature-relevant files (whole when small, signature
skeletons when large, 25k-char budget) + the **repo memory** (confirmed
facts from previous runs: fixed gate gotchas, approved-review notes). The
pack is computed **once per command** and reused across all attempts /
candidates / reviewer — the honest analogue, in this lane, of the paper's
prefix reuse (KV-cache).

The worker stops exploring the repo blindly — the biggest source of spend.
**Measured A/B on a real production repo (same feature, same conditions):
448.7s without pack → 128.6s with pack (−71%, 3.5× faster)** — and with
better placement (the packed worker followed the existing module's pattern;
the unpacked one invented the wrong directory). Duration is a direct proxy
for tokens in an agentic worker. Full data:
[2026-07-12-context-pack-ab-benchmark.md](2026-07-12-context-pack-ab-benchmark.md).
Opt-out: `--no-context-pack`.

### Multipliers

- **Retries add up**: each gate failure re-runs the whole worker (with the
  same pack — the build cost is not repeated).
- **Repo/feature size**: the worker is agentic; a broad feature = more tokens
  per session. The pack cuts exploration, not implementation.
- **Within each session**, the claude CLI already applies Anthropic's
  automatic prompt caching. What the paper does beyond that (byte-identical
  replay across sessions, ~95%) requires the framework's low-level lane +
  the per-token API — outside the subscription model; the context pack is
  this layer's answer.

### How to control it

- `--provider static` — rehearses the machinery at zero cost.
- `--no-review` — cuts the reviewer session.
- `--max-attempts 1`, `--max-repairs 0` — no retries.
- `--allowed-prefix` — confines the worker (and focuses the pack).
- `--best-of` and `optimize` are the most expensive; use them when the gain
  justifies it.
- Telemetry: each attempt records `duration_s` in the history
  (`~/.shepherd-dev/history/`) — the real spend per run is auditable.

## Limits & caveats

- **The worktree is the truth.** Each `run` recreates `.vcscore`; git is the
  durable state. It refuses to run with a pending proposal — settle first.
- **No file deletions** in this substrate version (add/modify only).
- **Large features:** raise `--worker-budget`.
- **Settlement is consume-once.**
- **Never use** raw `shepherd run select/apply` outside `settle`.
