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

## Using inside Cursor

There is no native Cursor plugin — shepherd-dev is IDE-agnostic. Two ways:

**1. The CLI in Cursor's integrated terminal (works today, nothing
Cursor-specific).** Install once and use it from the terminal panel exactly as
anywhere else:

```bash
uv tool install git+https://github.com/andrefogelman/shepherd.git
cd ~/projects/my-app && shepherd-dev init
shepherd-dev run "add CPF validation"
```

The accept/reject prompt works in the terminal. Full feature — remote gate,
accelerators, hard-kill.

**2. A Cursor rule so Cursor's Agent drives it.** Copy
[examples/cursor/shepherd-dev.mdc](../examples/cursor/shepherd-dev.mdc) into your
repo's `.cursor/rules/`. Then in Cursor chat: "develop X with shepherd" — the
Agent runs the CLI in the terminal, shows the report, and asks you to accept or
reject before settling.

Either way the worker is a headless `claude` session, so an authenticated
`claude` CLI is required — Cursor's own AI does not power the worker. Nothing
touches your files until you accept.

## Using inside Codex (or any MCP client)

Two ways, both cross-agent (they also work in Cursor, Claude Code, and the
ChatGPT desktop app).

**1. The skill (portable).** `codex/skills/shepherd-dev/SKILL.md` is the
[agentskills.io](https://agentskills.io) open standard — the same skill Codex,
Claude Code and Cursor read. Copy it into `~/.codex/skills/shepherd-dev/`
(personal) or `.codex/skills/shepherd-dev/` (project, version-controlled).
Restart Codex, then in chat: "develop X with shepherd" — the agent invokes the
skill, runs the CLI, shows the report, and asks you to accept or reject.

**2. The MCP server (native tools).** shepherd-dev ships an MCP stdio server —
`shepherd-dev mcp` — exposing `shepherd_run`, `shepherd_run2`, `shepherd_settle`,
`shepherd_settle_par`. Add it once to `~/.codex/config.toml`:

```toml
[mcp_servers.shepherd-dev]
command = "shepherd-dev"
args = ["mcp"]
```

(or `codex mcp add shepherd-dev -- shepherd-dev mcp`; verify with `codex mcp
list`). The same server works in Cursor (`.cursor/mcp.json`), Claude Code, and
the ChatGPT desktop app — one server, every client. `shepherd_run`/`run2` always
run with `--no-settle`, so nothing is applied through MCP; you settle
explicitly, and **accepting requires `confirm: true`** (the settle tools refuse
to write files without it). See [codex/README.md](../codex/README.md).

Verified Codex CLI gotcha: MCP tools are **deferred** behind tool search
(`tool_search_always_defer_mcp_tools`) — they do not appear in the model's
initial tool list. They surface as `mcp__shepherd_dev__shepherd_run` etc. once
the agent searches; if Codex claims it has no shepherd tools, ask it to search
its deferred tools for "shepherd".

By default the worker is a headless `claude` session — an authenticated `claude`
CLI is required; the host agent's own model does not power it. With
`--provider codex` or `--provider grok` no `claude` CLI is needed.

**Grok worker (no Claude):** pass `--provider grok`. The worker is the Grok Build
CLI (`grok` on PATH or `~/.grok/bin/grok`); proposals are staged for
`settle-par` (same as run2). See [2026-07-14-grok-provider-l1-l2.md](2026-07-14-grok-provider-l1-l2.md).
Default `--provider claude` is unchanged.

**Codex worker (no Claude, real LLM review):** pass `--provider codex`. The
worker is headless `codex exec` (`codex` on PATH; override `--codex-cmd` /
`SHEPHERD_DEV_CODEX_CMD`, model via `--codex-model`). Isolation is double: a
temp clone (L1) plus the Codex CLI's own OS sandbox (`--sandbox
workspace-write`, Seatbelt/Landlock). Unlike Grok's heuristic, Codex runs a
**real LLM review**: a second `codex exec` re-reads the modified clone in a
read-only sandbox and returns a structured JSON verdict — so `--auto-settle`
works on this provider. Running shepherd-dev INSIDE Codex closes a fully-Codex
loop: the host agent calls `shepherd_run` with `provider: "codex"` and the
worker is Codex too. See [2026-07-19-codex-provider.md](2026-07-19-codex-provider.md).

## Install (per machine)

```bash
uv tool install git+https://github.com/andrefogelman/shepherd.git
# or, with Claude Code (brings the skill + /shepherd-dev:* commands):
#   /plugin marketplace add andrefogelman/shepherd
#   /plugin install shepherd-dev@shepherd
```

Requirements: Python 3.11+, git, an authenticated `claude` CLI, macOS
(Seatbelt) or Linux with Landlock (kernel ≥ 5.13). Windows: WSL.

**Desktop notification.** When a `run`/`run2`/`runN` finishes, a native OS
notification tells you the proposal is ready to settle (or that the run
failed) — runs take minutes and the decision waits in a terminal you already
left. macOS and Linux (`notify-send`); best-effort, never affects the run.
Disable with `SHEPHERD_DEV_NO_NOTIFY=1`.

**Update notice.** When a new version is published, the end of your next
command shows `update available: shepherd-dev X.Y.Z … upgrade with:
shepherd-dev update` (on stderr). Zero cost on the command: the notice reads a
local cache; the cache refreshes in the background at most once a day. Then run
`shepherd-dev update` — a synchronous check + reinstall via uv, always your
decision (the tool never self-updates silently). Disable the notice with
`SHEPHERD_DEV_NO_UPDATE_CHECK=1`.

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
missing), shepherd uses a native runner — `node --test` (with strip-types for
`.ts` on Node ≥ 22.6), `python3 -m unittest`, `mix test` (Elixir) or
`cargo test` (Rust) — **and instructs the worker to write the tests alongside
the feature**. You write only the intent; the tests come in the package. A
guard rejects a proposal that ships no test (including Rust, where
`cargo test` would otherwise pass vacuously with 0 tests).

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
| `run2 "A" "B" --repo P --test-cmd "…"` | Two COUPLED features, two coordinated workers; conflict handoff; combined gate; winner staged for `settle-par`. |
| `runN "A" "B" ["C"…] --repo P` | 2–5 INDEPENDENT features in parallel lanes; OWN gate + review + proposal per lane; individual settle with a re-gate. |
| `run … --best-of K` | K candidates (2–4) from the same state; deterministic ranking; the best is staged. |
| `settle <run-ref> --repo P [--reject]` | Settles a `run` proposal. |
| `settle-par <proposal-id> --repo P [--reject]` | Settles a staged `run2` / `--best-of` proposal. |
| `init --repo P` | Initializes the repo (once). |
| `optimize [--apply]` | Improves the worker prompts from run history, validated by replay. |
| `trace [run-id\|last] [--full] [--json]` | Replays the step-by-step timeline of a run recorded with `-v`. |
| `status [--json] [--limit N]` | Ground-truth state of recent runs (running/succeeded/failed/stale, live phase and attempt) + pending staged proposals. `--json` for external UIs. |
| `update [--force]` | Updates to the published version (explicit, via uv — never self-updates silently). |

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
| `--no-plan` | run · run2 | Turns off the planning prefetch (no target/plan hints). |
| `--quiet` | run | Silences ALL live feedback (progress and verbose). |
| `--fresh-adopt` | run | Forces a full worktree re-adoption (skips the unchanged-worktree cache). |
| `--speculative-review` | run | Runs the reviewer in parallel with the gate (hides review latency; spends review tokens even when the gate fails). |
| `--no-verbose` | run · run2 | Turns off the step-by-step feed (phase progress only, no event log). |
| `--no-watchdog` | run | Turns off the worker budget hard-kill backstop. |

## Accelerators & robustness

Three mechanisms turn on by themselves (no setup) and make each run faster and
safer. All degrade cleanly: if something fails, the run proceeds as usual.

**Planning prefetch.** Before the worker starts, a quick pass with a cheap model
decomposes the feature into a plan and the exact target files, which feed the
context pack — the worker starts knowing where to touch instead of exploring the
repo from scratch. Best-effort: on any failure (no network, missing CLI) the run
continues. Disable with `--no-plan`; change the model via `planning.model` in
`.shepherd-dev.json`.

**Live feedback.** While the run happens, the terminal shows per-phase progress —
`attempt k/N · worker → gate → review` — with a spinner and elapsed time,
committing a `✓/✗` line as each phase settles. After each attempt, a summary of
what the worker did (files touched + a tool tally read from the run trace). On a
non-interactive terminal (CI) it degrades to plain lines. Silence with `--quiet`.

**Budget hard-kill.** `--worker-budget` (default 900s) is the wall-clock cap per
attempt. On expiry the whole worker is really killed — its entire process tree,
leaving no orphan — in two layers: (A) at the source, the worker's process group
is reaped on expiry; (B) an independent backstop guarantees the kill even if
layer A doesn't apply in your environment. A stuck worker dies at the budget, not
in an open-ended wait. Disable the backstop with `--no-watchdog`.

**Execution speed.** Four mechanisms cut the orchestration overhead (measured:
20.7s → 8.7s per consecutive run on a 1500-file repo): the adoption cache
(worktree unchanged since the last run → the re-adoption is skipped;
`--fresh-adopt` forces it), the pre-staged local gate (the base copy is built in
the background while the worker runs — via the filesystem's clonefile/reflink —
and each attempt only overlays the proposal's files), concurrent clone creation
in best-of/run2, and the opt-in speculative review (`--speculative-review`) that
runs the reviewer in parallel with the gate. Everything degrades cleanly: any
failure falls back to the full path.

## Verbose mode & trace (step by step)

The live step-by-step feed is `run`'s **default**: every tool the worker uses,
every edit with its diff (+/− lines), every gate output line and every failing
test appear as sub-lines under the live progress, in real time (turn off with
`--no-verbose`; `--quiet` silences everything):

```
⠹ attempt 1/3 · worker running · 2m14s
   ⚒ Read …/src/auth/signup.py
   ✎ …/src/auth/signup.py (+12 −3)
   ┆ collected 24 items
   ✗ tests/test_signup.py::test_cpf (pytest)
```

How it works: the jailed worker already emits a structured event stream; shepherd
tees it into the workspace scratch — an area scrubbed before the delta is
captured, so it can never leak into a proposal — and a thread tails the file
live. The per-edit diff comes for free from the Edit tool's own input (old/new);
a Write renders as a diff against the repo's current state. The gate (local AND
remote) runs streamed line by line, with parsers that name the failing test
(pytest, unittest, jest/vitest, ExUnit, cargo, go).

Everything is persisted as NDJSON in `~/.shepherd-dev/runs/<run-id>/events.ndjson`
and can be replayed later:

```bash
shepherd-dev trace last          # timeline of the most recent run
shepherd-dev trace <run-id>      # of a specific run
shepherd-dev trace last --full   # include EVERY gate output line
shepherd-dev trace last --json   # raw NDJSON (for machines)
```

With `--best-of K`, each candidate records its own log (`<id>-c0`, `<id>-c1`, …)
with no live rendering (K interleaved spinners would garble the terminal); use
`trace <id>-cK` afterwards.

On `run2` (also the default), each worker records its own log (`<id>-wa`,
`<id>-wb` — the handoff rework lands in the follower's) and a MAIN log (`<id>`)
carries the narrative: conflicts/handoff, the combined gate streamed line by
line, repair rounds, and the review. The main log's sequential phases render
live; the (concurrent) workers replay via `trace`. Everything is best-effort:
any failure of the mechanism turns off only the verbose feed — the run proceeds
intact.

## runN — independent features in parallel

For up to **5 INDEPENDENT features** at once (hard cap), each in an isolated
lane with its own full cycle — jailed worker → policy → **own gate** (with the
guided retry loop) → **own review** → **own staged proposal**. One failed lane
does not sink the others. Workers run concurrently (`--max-workers`, default 3,
max 5 — the practical ceiling is your subscription quota, not CPU); gates take
turns on a lock (the CPU-heavy part never runs doubled).

```bash
shepherd-dev runN "feature A" "feature B" "feature C" --repo ~/projects/my-app
# → 3 staged proposals; settle each one:
shepherd-dev settle-par <proposal-id> --repo ~/projects/my-app
```

**When runN vs run2.** The Shepherd paper proved that on COUPLED tasks
coordination matters enormously (CooperBench: 28.8% without vs 54.7% with the
supervisor) — that is `run2` territory (coordination notes, handoff, combined
gate). `runN` assumes genuine independence, and carries two automatic
methodology guardrails to stay faithful:

1. **Coupling warning at preflight**: the planning prefetch predicts each
   feature's target files; if the predictions intersect, runN warns BEFORE any
   worker spends tokens ("these features look coupled — use run2"). Real
   intersection across the finished proposals is also reported loudly.
2. **Re-gate at settle**: each runN proposal passed its gate against the base it
   was BUILT on — at settlement, `settle-par` re-runs the suite against the
   REAL current worktree (current base + proposal) before writing any file. If
   the base drifted (another settle in between) and tests break, the settlement
   is refused and the proposal stays staged: re-run the feature on the new
   base. The gate always judges what will actually exist.

Parallelism buys **wall-clock**, not tokens: three ~5-minute features finish in
~6–7 minutes total instead of ~15 sequential. Verbose by default: one log per
lane (`<id>-w0…`) plus a main narrative log; replay with `trace`.

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

## Remote gate (build/test on another host)

Some repos only build/test in an environment the local machine lacks — a
database, a container, another architecture, a GPU. The worker still runs
**locally** (it only edits files); the **gate** runs on an arbitrary remote host
over SSH. Shepherd knows no database or service — you describe it all in config
(`test_remote`):

```json
{ "test_remote": {
  "ssh": "user@host",
  "repo_dir": "/path/to/warm/checkout",
  "test_cmd": "<the gate command>",
  "setup_cmd": "<optional: bring up DB/containers/services>",
  "teardown_cmd": "<optional: tear them down — ALWAYS runs>",
  "writable": ["_build"],
  "env": { "DATABASE_URL": "postgres://localhost/app_{id}" }
} }
```

Every command and `env` value may reference `{id}` (a unique per-gate-run token)
and `{workdir}` (the ephemeral remote copy). That is how you isolate a stateful
service **without** shepherd knowing the service — name a per-`{id}` database /
compose project / container. It works with **any** database (Postgres, MySQL,
Mongo, Redis, SQLite), queue, or service — only your config text changes.

Per run, the gate: preflights SSH **before** spending a worker (fails clearly on
an offline host instead of burning attempts); makes an ephemeral copy of the
warm checkout (the warm one is never mutated); overlays the proposal's files;
runs `setup_cmd` → `test_cmd` (with a **remote** timeout) → `teardown_cmd`; and
cleans up **always**, even on timeout/error. Parallel modes (`run2`/`best-of`)
with a stateful service: use `{id}` in the config to run in parallel; without it,
shepherd serializes the remote gates so shared state can't be corrupted.

While the worker edits, shepherd already **pre-stages** the remote gate in
parallel (the ephemeral copy of the warm checkout and, when the config isolates
by `{id}`, the service `setup_cmd`) — so when the proposal is ready only the
overlay + test remain, and the staging latency is hidden behind the worker's
time. The staging never leaves residue: if the worker produces nothing, the
pre-staged workdir/service is torn down.

Optional keys: `copy_cmd` (default `cp -al {repo} {workdir}` — GNU/Linux
hardlink; override for BSD/macOS hosts, e.g. `rsync -a --link-dest={repo}
{repo}/ {workdir}/`), `workdir_base`, `ssh_opts`. The worker binary (editing) and
the test binary (remote) are independent; the sandbox's network does not affect
the gate.

### How it works underneath (and why it's safe)

Three points that commonly raise doubts:

- **It's not a flag** (`--remote`/`--ssh`/`--vm` don't exist). It's the
  `test_remote` config block in `.shepherd-dev.json`. If you looked for a flag and
  found none, that's why — the remote gate turns on by itself when the config is
  present.
- **The worker doesn't need the host's toolchain.** It runs in the local sandbox
  and only **edits files** — it doesn't compile, bring up a database, or run
  tests. Editing code doesn't require the stack. So your local machine may have no
  Docker, no database, not even that language's compiler — none of it blocks the
  worker.
- **The gate runs OUTSIDE the sandbox and tests the worker's REAL code.** The gate
  is a step in the shepherd process (not the sandboxed worker), so it has open
  network for SSH/rsync. Each run it **syncs the proposal the worker just produced**
  to the host (overlays the changed files onto an ephemeral copy of the warm
  checkout) and only then runs the tests. The host never tests a stale copy — it
  tests exactly what the worker proposed.

In short: local worker (cheap, no stack) + remote gate (in the full environment),
with the proposal synced automatically between the two. You write no sync or ssh
script — you only declare the host and the commands in config.

### Step by step

1. **Prepare a warm checkout on the host** — a clone of the repo with deps/build
   already compiled (`repo_dir`). Each gate run starts from it and never mutates it.
2. **Ensure passwordless SSH** — key/agent already set up (`ssh user@host` works
   on its own). Shepherd uses `BatchMode=yes`.
3. **Write `test_remote` in `.shepherd-dev.json`** (commit it — project metadata).
   Use `{id}` to isolate anything stateful.
4. **Run as usual**: `shepherd-dev run "<feature>"`. A preflight confirms the host
   before spending a worker; everything else is like the local flow.

### Recipes (change only the text — shepherd is agnostic)

**Elixir + Postgres in Docker Compose** (the `compose.yml` lives in the repo):

```json
{ "test_remote": {
  "ssh": "user@host", "repo_dir": "/srv/app",
  "setup_cmd": "docker compose -p sg-{id} up -d db && until docker compose -p sg-{id} exec -T db pg_isready; do sleep 1; done && MIX_ENV=test mix ecto.migrate",
  "test_cmd": "mix test",
  "teardown_cmd": "docker compose -p sg-{id} down -v",
  "writable": ["_build"],
  "env": { "MIX_ENV": "test", "DATABASE_URL": "postgres://postgres@localhost:5432/app_{id}" }
} }
```

**Rails + Postgres already running on the host** (no Docker):

```json
{ "test_remote": {
  "ssh": "ci@host", "repo_dir": "/home/ci/app",
  "setup_cmd": "createdb app_{id} && RAILS_ENV=test DB=app_{id} bin/rails db:schema:load",
  "test_cmd": "DB=app_{id} bundle exec rspec",
  "teardown_cmd": "dropdb app_{id}",
  "env": { "RAILS_ENV": "test" }
} }
```

**MySQL** (only the setup/teardown text changes):

```json
{ "test_remote": {
  "ssh": "user@host", "repo_dir": "/app",
  "setup_cmd": "mysql -e 'CREATE DATABASE app_{id}' && DB=app_{id} npm run migrate",
  "test_cmd": "DB=app_{id} npm test",
  "teardown_cmd": "mysql -e 'DROP DATABASE app_{id}'"
} }
```

**Testcontainers / a service the test brings up itself** (no external
setup/teardown):

```json
{ "test_remote": { "ssh": "user@host", "repo_dir": "/app", "test_cmd": "go test ./..." } }
```

Redis, MongoDB, SQL Server, Kafka, cross-compile, GPU: same pattern — `setup_cmd`
brings it up, `teardown_cmd` tears it down, `{id}` isolates. Shepherd doesn't change.

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
