# Grok provider (L1 host + L2 lane try)

**Status:** shipped as opt-in `--provider grok`  
**Invariant:** default `--provider claude` is unchanged. Claude Code plugin, jail, killtree, and prompts are not rewritten for Grok.

## What works without Claude

```bash
shepherd-dev run "add a hello.py that prints hi" \
  --provider grok \
  --repo /path/to/workspace \
  --test-cmd "true" \
  --no-review
```

- Worker: **Grok Build CLI** (`grok` on PATH or `~/.grok/bin/grok`, override `SHEPHERD_DEV_GROK_CMD`)
- Isolation: temp clone of the repo (L1 host)
- Policy + test gate + stage under `.shepherd-proposals/<id>/`
- Settle: `shepherd-dev settle-par <id>` (same as run2/best-of)
- No `claude` subprocess on this path

## Flags

| Flag | Meaning |
|------|---------|
| `--provider grok` | Use Grok instead of Claude |
| `--worker-backend auto\|host\|lane` | L1 host only, or try L2 transport rebind then host |
| `--grok-cmd` / `SHEPHERD_DEV_GROK_CMD` | Grok binary |
| `--grok-model` / `SHEPHERD_DEV_GROK_MODEL` | Model id |
| `--no-review` | Skip heuristic review (default for experimentation; auto-settle needs review) |

## L1 vs L2

| | L1 host (default) | L2 lane |
|--|-------------------|---------|
| Mechanism | Clone → `grok --cwd clone …` → diff → gate → stage | Try rebind of shepherd-ai Claude transport slot to launch Grok |
| Settlement | `settle-par` | Same stage UX (live workspace.run only if `SHEPHERD_DEV_GROK_LANE_LIVE=1`) |
| Needs Claude CLI | **No** | **No** |
| Needs shepherd-ai | Only if repo already is a Shepherd workspace for `init`/`.vcscore` discovery | Same + experimental live lane |

L2 on shepherd-ai 0.3.0 only exposes a `claude` transport slot. We can install a Grok-launching provider into that slot when `--provider grok` is set; the default Claude path never installs it and restores previous transports after the run.

## Offline / tests

```bash
SHEPHERD_DEV_GROK_FAKE=1 shepherd-dev run "x" --provider grok --test-cmd true --no-review
# or inject FakeGrokExecutor in unit tests
```

## What is NOT changed

- Default provider remains `claude`
- `set_worker_budget` / killtree / watchdog Claude markers
- MCP settle `confirm=true`
- Claude Code skill and slash commands (they keep invoking default claude)
