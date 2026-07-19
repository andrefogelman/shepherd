# shepherd-dev in Codex (and any MCP client)

shepherd-dev is IDE/agent-agnostic — a CLI plus an open-standard skill. Two ways to wire it
into OpenAI Codex CLI (both also work in Cursor, Claude Code, and the ChatGPT desktop app).

First install the CLI once per machine:

```bash
uv tool install git+https://github.com/andrefogelman/shepherd.git
```

Requires Python 3.11+ and git. The default worker is a headless Claude Code session
(needs an authenticated `claude` CLI); the host agent's own model does not power the
worker. With `--provider codex` the worker (and a real LLM reviewer) is the Codex CLI
itself — no `claude` needed; `--provider grok` likewise uses only the Grok CLI.

## Option A — the skill (portable, recommended)

`skills/shepherd-dev/SKILL.md` here is the [agentskills.io](https://agentskills.io) open
standard — the same file Claude Code, Cursor, and Codex all read. Drop it in:

```bash
# personal (all repos):
mkdir -p ~/.codex/skills/shepherd-dev && cp skills/shepherd-dev/SKILL.md ~/.codex/skills/shepherd-dev/
# or project-scoped (version-controlled, shared with your team):
mkdir -p .codex/skills/shepherd-dev && cp skills/shepherd-dev/SKILL.md .codex/skills/shepherd-dev/
```

Restart Codex (skills load at session start). Then in Codex chat: "develop X with shepherd" —
the agent invokes the skill (implicit match on the description, or explicit via `/skills`),
runs the CLI in the terminal, shows the report, and asks you to accept or reject before
settling. Codex ignores the Claude-specific `openai.yaml`; none is needed.

## Option B — the MCP server (native tools, universal)

shepherd-dev ships an MCP stdio server (`shepherd-dev mcp`) that exposes `shepherd_run`,
`shepherd_run2`, `shepherd_settle`, `shepherd_settle_par` as native tools. Add it to
`~/.codex/config.toml` (or a trusted project's `.codex/config.toml`):

```toml
[mcp_servers.shepherd-dev]
command = "shepherd-dev"
args = ["mcp"]
```

Or: `codex mcp add shepherd-dev -- shepherd-dev mcp`. Verify with `codex mcp list` (or
`/mcp` in a session). The same server works in Cursor and Claude Code (`.cursor/mcp.json`,
Claude MCP config) and the ChatGPT desktop app — one server, every client.

Verified gotcha: in the Codex CLI, MCP tools are **deferred** behind tool search
(`tool_search_always_defer_mcp_tools`) — they don't show in the model's initial tool list
and surface as `mcp__shepherd_dev__shepherd_run` etc. only when the agent searches. If
Codex claims it has no shepherd tools, tell it to search its deferred tools for "shepherd".

Fully-Codex loop: from inside Codex, call `shepherd_run` with `provider: "codex"` — the
worker (and the LLM reviewer) is the Codex CLI itself, sandboxed in an isolated clone,
and the proposal still waits for your explicit settle.

`shepherd_run`/`shepherd_run2` always run with `--no-settle`: nothing is applied through
MCP. The agent reports the retained proposal and you settle it explicitly with
`shepherd_settle` / `shepherd_settle_par`. **Accepting requires `confirm: true`** — the
settle tools refuse to write files without it (rejecting is safe and needs none), so a
client cannot silently apply a proposal. Human-only settlement is enforced in the protocol,
not just by convention.
