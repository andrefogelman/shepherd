# Nothing Touches Your Files Until You Say So

*Why I stopped letting AI coding agents write directly to my repo — and built a supervised layer that gates every change on your own tests, runs a skeptical reviewer, and hands the decision back to you.*

*By Andre Fogelman · ~11 min read · Open source (MIT)*

---

Here's a scene every AI-assisted developer knows. You ask an agent for a small feature. It thinks for a while, edits nine files, runs some commands, and announces success. You open the diff. Three of the nine files are the feature. Two are a "refactor" you never asked for. One quietly changed a config. And a test somewhere is now failing — you're not sure if it was already failing or if the agent broke it, because the agent already *applied* everything to your working tree.

When it works, it feels like magic. When it doesn't, you're doing forensic archaeology on changes that are already live in your repo.

The reflex is to blame the model. But the model isn't the problem. A brilliant engineer who commits their own unreviewed code straight to `main`, applies changes before the tests run, and edits files nobody asked about would also be a problem — not because they're bad at coding, but because there's **no process around them**. We built elaborate processes for humans (pull requests, CI gates, code review, staged rollouts) precisely because talent without process is dangerous. Then we handed AI agents the keyboard with none of it.

So I built **shepherd-dev**: a supervised layer for AI development, on top of the [Shepherd](https://arxiv.org/abs/2605.10913) framework. The one-line version: *a sandboxed worker proposes a change; a deterministic supervisor gates it on your repo's own tests, runs a skeptical reviewer, and holds everything for you. Nothing touches your files until you accept.*

## Supervision, not autonomy

The entire design collapses to a single principle: **separate the creative act from the decision to apply it.** Everything in the tool is downstream of that.

```
Worker (implements) → Policy (guards) → Gate (tests) → Reviewer (audits) → YOU (settle)
```

Walk the pipeline:

- **Worker.** A Claude Code session runs *inside a sandbox* — macOS Seatbelt or Linux Landlock. It edits files only in an isolated workspace, with no network and no path to your real working tree. It cannot, by construction, touch your repo.
- **Policy.** Deterministic guards, written in code, not model judgment: forbidden paths (`.env`, `.git/`, `node_modules/`, anywhere they're nested), a cap on how many files a change may touch, and a hard rejection of any path that tries to climb out of the repo.
- **Gate.** Your repository's *actual test suite*. If it fails, the worker doesn't just try again blindly — it retries with the real error output injected as structured guidance, plus its own previous attempt, so it iterates instead of restarting.
- **Reviewer.** A separate, skeptical pass that reads the proposed change and returns `APPROVED` or `REJECTED` with specific, file-level issues. It is explicitly told to be a rigorous critic and never to rubber-stamp.
- **You.** A proposal that passes stays *retained* under a reference. It becomes real files in your worktree only when you run `settle`. Even the git commit stays yours.

That final step is the whole product. Call it **human-only settlement**. The machinery can propose, test, and critique all day long; the act of applying is always, unavoidably, your decision.

## Why the supervisor is code, not another model

There's a strong gravitational pull, right now, toward making *everything* an agent. Route requests with an LLM. Decide retries with an LLM. Rank outputs with an LLM. It's seductive and it's a trap: it's non-deterministic, it's expensive, and — worst of all — it launders errors through a second layer of probability where you can no longer see them.

shepherd's supervisor is plain, boring Python. Forking the workspace, checking a changeset against policy, running the gate, ranking candidates, guaranteeing teardown even on failure — none of that requires a model. The rule I kept coming back to: **if code can answer, code answers.** The LLM is spent only where judgment genuinely lives — implementing a feature, reviewing a diff. Everything around it is deterministic, unit-tested, and costs exactly zero tokens.

This isn't asceticism. It's what makes the guarantees real. "Nothing is applied until you accept" is only trustworthy if the thing enforcing it is a line of code you can read, not a prompt you hope the model respected.

## The gate is your tests — and if you have none, it writes them

The objective arbiter of "did this work?" is your own suite. `pytest`, `npm test`, `mix test`, `cargo test` — whatever you already run. shepherd auto-detects it the first time you initialize a repo and saves it, so day to day you type only the intent:

```bash
shepherd-dev run "add CPF validation to signup"
```

But most interesting repos, at some point, are repos *without* a usable suite — a fresh project, a prototype, a package whose `node_modules` aren't installed. Rather than pretend, shepherd drops to a **zero-dependency native gate** (`node --test`, `python -m unittest`, `mix test`, `cargo test`) and instructs the worker to *write the tests alongside the feature*. You supply the intent; the tests come in the package.

There's a subtle failure mode here worth naming, because it's the kind of thing that quietly rots a test gate: some runners *pass with zero tests*. `cargo test` and `mix test` both exit happily on an empty suite. A gate that passes vacuously is worse than no gate — it's a green light with nothing behind it. So the native gate uses a presence sentinel: a proposal that ships no real test for that language is rejected loudly, not waved through.

## The reviewer, and the custody of trust

A passing test suite tells you the change didn't break anything measurable. It doesn't tell you the change is *good* — that it's scoped, idiomatic, free of hidden bugs and security holes, and that it didn't sneak in an unrequested "improvement." That's the reviewer's job.

The reviewer runs in strict isolation. It reads the current (pre-change) code plus the full proposed diff, and it may write exactly one file: its verdict. A deterministic guard checks that it touched nothing else — if it did, the verdict is thrown out. Its output is read and then discarded. The point is custody: the critic never gets to become an author.

And when the gate passes but the reviewer says *reject*, shepherd doesn't hide it behind a cheerful "succeeded." It warns you, loudly, before you accept — because a green gate is not an approval, and accepting a rejected proposal should be a conscious choice, never a reflex.

## Making supervision cheap and fast

Supervision that triples your cost is a hard sell, so a lot of the engineering went into the opposite direction.

An agentic worker's single largest expense is *blind exploration* — `Read` after `Read`, trying to reconstruct the shape of a codebase it's seeing for the first time. So before the worker ever starts, shepherd builds a **context pack** locally, at zero token cost and in about two seconds: the file tree, the files most relevant to the feature (whole when they're small, signature-only skeletons when they're large), and the repo's learned memory from previous runs. It's computed once per command and reused across every retry and every candidate.

The measured effect, on a real production repository, same feature, same conditions:

> **448.7s → 128.6s** with the context pack. A 71% cut, 3.5× faster — and better placement: the packed worker followed the existing module's conventions, while the unpacked one invented the wrong directory entirely.

A cheap-model **planning prefetch** pushes it further: a fast pass names the exact target files up front, so the worker starts already knowing where to touch. And because the orchestration itself burns no tokens, the only spend is the model sessions you'd have paid for anyway — running against a subscription's quota, not a pay-per-token meter.

## The parts toy demos skip

It's easy to build something that supervises a to-do app. The interesting failures live in the boring, load-bearing parts of real work.

**Your tests only run somewhere else.** A database, a container, a different CPU architecture, a GPU. shepherd's **remote gate** keeps the worker local — it only edits files, so it needs none of that toolchain — and runs the gate on any host over SSH. You describe the whole environment in config; shepherd itself knows nothing about your database or service:

```json
{ "test_remote": {
  "ssh": "user@host",
  "repo_dir": "/warm/checkout",
  "setup_cmd": "docker compose -p sg-{id} up -d db",
  "test_cmd": "mix test",
  "teardown_cmd": "docker compose -p sg-{id} down -v",
  "env": { "DATABASE_URL": "postgres://localhost/app_{id}" }
} }
```

The `{id}` token is a unique per-run identifier, so a stateful service — a database, a compose project, a container — is isolated per run and parallel runs can't corrupt each other's state. shepherd achieves that without ever knowing what Postgres *is*; the isolation lives entirely in text you wrote. And while the worker is still editing, shepherd pre-stages the remote copy and setup in parallel, so the gate is ready the moment the proposal lands.

**A worker that never stops.** Give an agent a fifteen-minute budget and, occasionally, it will spend forty — reading, second-guessing, looping. The framework's own timeout only killed the launcher process, orphaning the real worker and its child processes, which kept running until a global safety timeout finally reaped the whole tree with nothing to show for it. shepherd now kills the *entire process group* at the budget — a `setsid` plus a group kill at the source, backed by an independent watchdog. A stuck worker dies at the budget, tree and all, leaving no orphans. I validated it in the live sandbox: a worker on a twenty-second budget was reaped at twenty-one seconds, zero survivors.

**One shot is a coin flip.** Model output is a distribution, so betting the whole run on a single sample is leaving quality on the table. `--best-of K` branches K candidates from the same starting state, each with a different emphasis — smallest diff, defensive robustness, matching the codebase's idioms — gates and reviews all of them, and stages the winner by a deterministic ranking (passed the gate, then approved, then fewest issues, then smallest diff). It's the essence of the paper's Tree-RL, applied at inference time, with no training loop.

## Use it from wherever you already work

shepherd-dev is a CLI, so it works in any terminal on its own. But it also meets your agent where it already lives:

- **Claude Code** — a plugin with a skill and slash commands (`/shepherd-dev:run`), so you drive it from the conversation.
- **Cursor** — the CLI in the integrated terminal, or a project rule that lets Cursor's own agent invoke it.
- **Codex, Cursor, the ChatGPT desktop app, Claude** — a built-in **MCP server** (`shepherd-dev mcp`) exposes `shepherd_run`, `shepherd_settle` and friends as native tools. One server, every client.

The skill is written to the open [agentskills.io](https://agentskills.io) standard, so the same file works across Claude Code, Cursor, and Codex unchanged. And the core invariant survives every one of these surfaces: even over MCP, accepting a proposal requires an explicit `confirm: true`. A tool call cannot silently write to your repository — the human-in-the-loop is enforced in the protocol itself, not left to good manners.

## What it isn't

Honesty ages better than hype, so the boundaries, plainly:

- The worker is a headless Claude Code session (a Grok backend is now optional too). shepherd *supervises* a model; it is not one. If the model can't build the feature, no amount of gating conjures it.
- The current substrate is add-and-modify only — a worker can't yet express file *deletions*.
- Every run re-adopts your working tree from scratch; git is the durable source of truth. It refuses to start while an unsettled proposal is still pending — you clear the deck first.
- It is deliberately *not* autonomous. That's not a missing feature. That's the thesis.

## Try it

It's MIT-licensed and open. Three lines to a supervised run:

```bash
uv tool install git+https://github.com/andrefogelman/shepherd.git
cd your-repo && shepherd-dev init
shepherd-dev run "the thing you actually want built"
```

I'll make a prediction. The best AI coding tools of the next couple of years won't be the most autonomous ones — the ones that edit the most files with the least friction. They'll be the most *supervised* ones: the ones that give the model real room to propose, and give *you* an unbreakable, legible grip on what actually ships.

Give the model room to propose. Keep the decision to apply. Nothing touches your files until you say so.

---

*shepherd-dev is built on Shepherd ([arXiv 2605.10913](https://arxiv.org/abs/2605.10913)). Source, docs, and the MCP server: [github.com/andrefogelman/shepherd](https://github.com/andrefogelman/shepherd). MIT-licensed.*
