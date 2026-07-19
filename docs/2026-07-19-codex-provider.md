# Codex provider (L1 host + L2 lane try + real LLM review)

**Status:** shipped as opt-in `--provider codex`
**Invariant:** default `--provider claude` is unchanged. Claude Code plugin, jail, killtree, and prompts are not rewritten for Codex. The Grok provider is unchanged in behaviour (its host loop now lives in the shared `providers/hosted.py`).

## What works without Claude

```bash
shepherd-dev run "add a hello.py that prints hi" \
  --provider codex \
  --repo /path/to/workspace \
  --test-cmd "true"
```

- Worker: **OpenAI Codex CLI** headless mode (`codex exec`; binary `codex` on PATH,
  override `--codex-cmd` / `SHEPHERD_DEV_CODEX_CMD`)
- Isolation is **double**: temp clone of the repo (L1 host) + the Codex CLI's own
  OS sandbox (`--sandbox workspace-write` — Seatbelt on macOS, Landlock on Linux)
  around every model-generated shell command
- Policy + test gate + stage under `.shepherd-proposals/<id>/`
- **Real LLM review** (unlike Grok's heuristic): after the gate passes, a second
  `codex exec` re-reads the modified clone in a `--sandbox read-only` session and
  returns a structured JSON verdict (`approved` / `summary` / `issues`), so
  `--auto-settle` is meaningful on this provider
- Settle: `shepherd-dev settle-par <id>` (same as run2/best-of)
- No `claude` subprocess on this path

## Flags

| Flag | Meaning |
|------|---------|
| `--provider codex` | Use Codex instead of Claude |
| `--worker-backend auto\|host\|lane` | L1 host only, or try L2 transport rebind then host |
| `--codex-cmd` / `SHEPHERD_DEV_CODEX_CMD` | Codex binary |
| `--codex-model` / `SHEPHERD_DEV_CODEX_MODEL` | Model id for `codex exec -m` |
| `SHEPHERD_DEV_CODEX_SANDBOX` | Worker sandbox policy (default `workspace-write`) |
| `--no-review` | Skip the Codex LLM review (heuristic/none; auto-settle then refuses) |

## Worker invocation

The executor launches:

```
codex exec -C <clone> --sandbox workspace-write --skip-git-repo-check \
  --ephemeral --color never [-m <model>] "<prompt>"
```

`codex exec` is non-interactive by design (no approval prompts); writes are
confined to the clone by the sandbox policy. `--skip-git-repo-check` is needed
because the L1 clone strips `.git`; `--ephemeral` keeps Codex session files off
disk.

## Review invocation

```
codex exec -C <clone> --sandbox read-only --skip-git-repo-check --ephemeral \
  --color never -o <last-message-file> [-m <model>] "<review prompt>"
```

The reviewer prompt lists the changed files and demands a bare JSON object
(`{"approved": …, "summary": …, "issues": […]}`); parsing is strict-first with
an embedded-JSON fallback. Any failure (missing binary, non-zero exit, timeout,
unparseable verdict) degrades to a `ReviewVerdict` with `error` set — an errored
review never counts as approval. The live review only engages when the real CLI
drives the worker; with an injected/fake executor the deterministic heuristic
runs instead (tests stay offline).

## L1 vs L2

| | L1 host (default) | L2 lane |
|--|-------------------|---------|
| Mechanism | Clone → `codex exec -C clone …` → diff → gate → review → stage | Try rebind of shepherd-ai Claude transport slot to launch Codex |
| Settlement | `settle-par` | Same stage UX (live workspace.run only if `SHEPHERD_DEV_CODEX_LANE_LIVE=1`) |
| Needs Claude CLI | **No** | **No** |
| Needs shepherd-ai | Only if repo already is a Shepherd workspace for `init`/`.vcscore` discovery | Same + experimental live lane |

L2 on shepherd-ai 0.3.0 only exposes a `claude` transport slot. We can install a
Codex-launching provider into that slot when `--provider codex` is set; the
default Claude path never installs it and restores previous transports after the
run.

## Shared hosted loop

`providers/hosted.py` is the provider-agnostic extraction of the original Grok
host path: `HostedReport`, `develop_hosted` (isolate → execute → policy → gate →
optional review_fn → stage), clone/prompt/heuristic-review helpers, and the
`HostedExecutor` protocol. `grok_host.develop_grok` and
`codex_host.develop_codex` are thin delegations; `GrokHostReport` remains a
back-compat alias of `HostedReport`.

## Offline / tests

```bash
SHEPHERD_DEV_CODEX_FAKE=1 shepherd-dev run "x" --provider codex --test-cmd true --no-review
# or inject FakeCodexExecutor / a fake review runner in unit tests
```

## What is NOT changed

- Default provider remains `claude`
- `set_worker_budget` / killtree / watchdog Claude markers
- MCP settle `confirm=true`
- Claude Code skill and slash commands (they keep invoking default claude)
- Grok provider behaviour (heuristic review only, same flags)
