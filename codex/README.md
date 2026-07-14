# shepherd-dev in Codex (and any MCP client)

shepherd-dev is IDE/agent-agnostic — a CLI plus an open-standard skill. Two ways to wire it
into OpenAI Codex CLI (both also work in Cursor, Claude Code, and the ChatGPT desktop app).

First install the CLI once per machine:

```bash
uv tool install git+https://github.com/andrefogelman/shepherd.git
```

Requires Python 3.11+, git, and an authenticated `claude` CLI — the worker is a headless
Claude Code session; the host agent's own model does not power the worker.

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

Or: `codex mcp add shepherd-dev -- shepherd-dev mcp`. Verify with `/mcp` in a Codex session.
The same server works in Cursor and Claude Code (`.cursor/mcp.json`, Claude MCP config) and
the ChatGPT desktop app — one server, every client.

`shepherd_run`/`shepherd_run2` always run with `--no-settle`: nothing is applied through
MCP. The agent reports the retained proposal and you settle it explicitly with
`shepherd_settle` / `shepherd_settle_par`. **Accepting requires `confirm: true`** — the
settle tools refuse to write files without it (rejecting is safe and needs none), so a
client cannot silently apply a proposal. Human-only settlement is enforced in the protocol,
not just by convention.
