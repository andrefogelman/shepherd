"""MCP stdio server: expose shepherd-dev as native tools for any MCP client
(Codex CLI, Cursor, Claude Code, the ChatGPT desktop app).

Zero dependencies — a minimal JSON-RPC 2.0 loop over stdio (newline-delimited),
in keeping with the rest of the package. Each tool shells out to the same
`shepherd-dev` CLI, so all the real logic (policy, gate, reviewer, retention)
is reused. `run`/`run2` are forced to `--no-settle`: nothing is ever applied
through MCP — the agent reports the retained proposal and the human settles it
explicitly via the settle tools. Human-only settlement is preserved.

Wire-up: `shepherd-dev mcp`. In Codex, add to ~/.codex/config.toml:

    [mcp_servers.shepherd-dev]
    command = "shepherd-dev"
    args = ["mcp"]
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import IO

from . import __version__

_PROTOCOL_VERSION = "2025-06-18"

# Each tool maps to a shepherd-dev subcommand. run/run2 always add --no-settle.
TOOLS = [
    {
        "name": "shepherd_run",
        "description": (
            "Develop one feature under supervision: a sandboxed worker implements it, "
            "the repo's test gate + a reviewer judge it, and the passing proposal is "
            "RETAINED (never auto-applied). Returns the report + a run-ref. Show the "
            "user the report, then call shepherd_settle to accept or reject."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "feature": {"type": "string", "description": "the feature in natural language"},
                "repo": {"type": "string", "description": "target repo path (default: cwd)"},
                "test_cmd": {"type": "string", "description": "gate command (default: saved/detected/native)"},
                "mode": {"type": "string", "enum": ["feature", "tests"], "description": "'tests' = only write tests"},
                "best_of": {"type": "integer", "description": "K parallel candidates (2-4)"},
                "allowed_prefix": {"type": "array", "items": {"type": "string"}, "description": "confine changes to these path prefixes"},
                "max_attempts": {"type": "integer", "description": "attempts before giving up (default 3)"},
                "provider": {
                    "type": "string",
                    "enum": ["claude", "static", "grok"],
                    "description": "worker backend; default claude. grok runs without the Claude CLI",
                },
            },
            "required": ["feature"],
        },
    },
    {
        "name": "shepherd_run2",
        "description": (
            "Develop two features with two coordinated parallel workers (conflict handoff, "
            "combined gate + review). The winner is STAGED (never auto-applied). Returns the "
            "report + a proposal-id; settle with shepherd_settle_par."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "feature_a": {"type": "string"},
                "feature_b": {"type": "string"},
                "repo": {"type": "string"},
                "test_cmd": {"type": "string"},
            },
            "required": ["feature_a", "feature_b"],
        },
    },
    {
        "name": "shepherd_settle",
        "description": "Settle a retained `run` proposal: accept (writes the files into the worktree) or reject (discard). Accepting requires confirm=true — a human decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_ref": {"type": "string", "description": "the run-ref from shepherd_run"},
                "repo": {"type": "string"},
                "reject": {"type": "boolean", "description": "discard instead of accepting"},
                "confirm": {"type": "boolean", "description": "required to ACCEPT (write files); the user's explicit decision"},
            },
            "required": ["run_ref"],
        },
    },
    {
        "name": "shepherd_settle_par",
        "description": "Settle a staged `run2` / best-of proposal: accept or reject. Accepting requires confirm=true — a human decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "repo": {"type": "string"},
                "reject": {"type": "boolean"},
                "confirm": {"type": "boolean", "description": "required to ACCEPT (write files); the user's explicit decision"},
            },
            "required": ["proposal_id"],
        },
    },
]

_TOOL_NAMES = {t["name"] for t in TOOLS}


def _shepherd_bin() -> str:
    """The shepherd-dev executable — the one that launched us, else on PATH."""
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and argv0.endswith("shepherd-dev"):
        return argv0
    return shutil.which("shepherd-dev") or "shepherd-dev"


def _argv_for(name: str, a: dict) -> list[str]:
    """Build the shepherd-dev CLI argv for an MCP tool call."""
    if name == "shepherd_run":
        # --no-verbose: the CLI defaults to the verbose step-by-step feed, but
        # over MCP the combined output IS the tool result — keep it compact.
        argv = ["run", str(a["feature"]), "--no-settle", "--no-verbose"]
        if a.get("repo"):
            argv += ["--repo", str(a["repo"])]
        if a.get("test_cmd"):
            argv += ["--test-cmd", str(a["test_cmd"])]
        if a.get("mode") == "tests":
            argv += ["--mode", "tests"]
        if a.get("best_of"):
            k = int(a["best_of"])
            if 2 <= k <= 4:  # the CLI only accepts 2-4; ignore out-of-range instead of leaking a hard error
                argv += ["--best-of", str(k)]
        for pfx in a.get("allowed_prefix") or []:
            argv += ["--allowed-prefix", str(pfx)]
        if a.get("max_attempts"):
            argv += ["--max-attempts", str(int(a["max_attempts"]))]
        prov = a.get("provider")
        if prov in ("claude", "static", "grok"):
            argv += ["--provider", str(prov)]
        return argv
    if name == "shepherd_run2":
        argv = ["run2", str(a["feature_a"]), str(a["feature_b"]), "--no-settle"]
        if a.get("repo"):
            argv += ["--repo", str(a["repo"])]
        if a.get("test_cmd"):
            argv += ["--test-cmd", str(a["test_cmd"])]
        return argv
    if name == "shepherd_settle":
        argv = ["settle", str(a["run_ref"])]
        if a.get("repo"):
            argv += ["--repo", str(a["repo"])]
        if a.get("reject"):
            argv += ["--reject"]
        return argv
    if name == "shepherd_settle_par":
        argv = ["settle-par", str(a["proposal_id"])]
        if a.get("repo"):
            argv += ["--repo", str(a["repo"])]
        if a.get("reject"):
            argv += ["--reject"]
        return argv
    raise KeyError(name)


def _run_cli(argv: list[str], timeout: int = 3600) -> tuple[int, str]:
    """Run `shepherd-dev <argv>` and return (exit_code, combined_output)."""
    proc = subprocess.run(
        [_shepherd_bin(), *argv],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _call_tool(name: str, arguments: dict) -> dict:
    """Dispatch a tools/call. Returns the JSON-RPC result payload."""
    if name not in _TOOL_NAMES:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
    # Human-only settlement (#4): accepting via MCP writes files, so the protocol
    # itself requires an explicit confirm=true — a client can't silently apply a
    # proposal. Rejecting (discarding) is safe and needs no confirmation.
    if name in ("shepherd_settle", "shepherd_settle_par") and not arguments.get("reject"):
        if arguments.get("confirm") is not True:
            ref = arguments.get("run_ref") or arguments.get("proposal_id") or "the proposal"
            return {"content": [{"type": "text", "text": (
                f"Accepting writes files into the worktree and needs explicit confirmation. "
                f"To ACCEPT {ref}, call this tool again with confirm=true. To discard it, pass "
                f"reject=true. Nothing was applied — surface this to the user and get their decision."
            )}], "isError": False}
    try:
        argv = _argv_for(name, arguments or {})
    except KeyError as exc:
        return {"content": [{"type": "text", "text": f"missing required argument: {exc}"}], "isError": True}
    try:
        code, out = _run_cli(argv)
    except subprocess.TimeoutExpired:
        return {"content": [{"type": "text", "text": "shepherd-dev timed out"}], "isError": True}
    except OSError as exc:
        return {"content": [{"type": "text", "text": f"could not run shepherd-dev: {exc}"}], "isError": True}
    return {"content": [{"type": "text", "text": out or "(no output)"}], "isError": code != 0}


def _ok(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(msg: dict) -> dict | None:
    """Handle one JSON-RPC message. Returns a response dict, or None for a
    notification (no reply)."""
    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = msg_id is None

    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion") or _PROTOCOL_VERSION
        return _ok(msg_id, {
            "protocolVersion": want,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "shepherd-dev", "version": __version__},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        return _ok(msg_id, _call_tool(params.get("name", ""), params.get("arguments") or {}))
    if is_notification:
        return None
    return _err(msg_id, -32601, f"method not found: {method}")


def serve(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout.

    Uses readline() in a loop, NOT `for line in stdin`: the file-iterator has a
    hidden read-ahead buffer that stalls a long-lived request/response pipe (a
    real MCP client keeps stdin open and sends one request at a time, so the
    iterator would block waiting to fill its buffer and never answer the
    handshake). readline() returns each message as soon as its newline arrives.
    """
    src: IO[str] = stdin if stdin is not None else sys.stdin
    dst: IO[str] = stdout if stdout is not None else sys.stdout
    while True:
        raw = src.readline()
        if raw == "":  # EOF — the client closed the pipe
            break
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            dst.write(json.dumps(_err(None, -32700, "parse error")) + "\n")
            dst.flush()
            continue
        response = handle_message(msg)
        if response is not None:
            dst.write(json.dumps(response) + "\n")
            dst.flush()
    return 0
