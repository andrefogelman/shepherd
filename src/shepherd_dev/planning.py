"""Planning prefetch (#4): a cheap-model pass that decomposes the feature into a
short plan + the exact target files, fed to the worker so it skips the "where do
I even touch" exploration (the dominant wall-clock cost).

Runs the `claude` CLI in the PARENT process — outside the sandbox jail, where
network is allowed, exactly like the gate. Because nothing jails it, it gets the
worker's isolation instead: HOME, CLAUDE_CONFIG_DIR and TMPDIR redirected into a
scratch that holds only the seeded login, the worker's hardening flags, and only
the read-only tools (Read, Glob, Grep) so it can open the repository's files.
Neither the repository's CLAUDE.md, settings and MCP servers nor the user's own
reach it. Best-effort by design: any failure
(no CLI, timeout, bad JSON, unknown paths) yields an empty plan and the run
proceeds on keyword-scored targets exactly as before. No new dependency: it
reuses the `claude` CLI the worker already requires.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import programs
from .launch import HARDENING_FLAGS

DEFAULT_PLAN_MODEL = "claude-haiku-4-5-20251001"
PLAN_TIMEOUT = 60          # seconds for the planning call
MAX_TARGETS = 8            # cap on planned target files

#: The worker's hardening (no MCP servers, no browser bridge, user settings
#: only) plus the read-only tools, so the planner can open files but not change
#: or run anything. `--tools` takes a variadic list, so it stays last, after the
#: prompt.
PLAN_ISOLATION: tuple[str, ...] = (*HARDENING_FLAGS, "--tools", "Read,Glob,Grep")


@dataclass
class PlanResult:
    targets: list[str] = field(default_factory=list)
    plan: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (bool(self.targets) or bool(self.plan))


def _norm(rel: str) -> str:
    return Path(rel).as_posix()


def build_plan_prompt(feature: str, file_tree_text: str) -> str:
    """The planning prompt. Deterministic; the model must reply with JSON only."""
    return (
        "You are planning a focused code change in an existing repository.\n"
        "Repository files:\n"
        f"{file_tree_text}\n\n"
        f"Feature to implement:\n{feature}\n\n"
        'Reply with MINIFIED JSON ONLY, no prose, no code fence: '
        '{"targets":["path",...],"plan":"3-6 terse imperative steps"}\n'
        f"targets = the EXACT existing repo paths most likely to change (max {MAX_TARGETS}), "
        "each copied verbatim from the list above. plan = a short ordered sketch."
    )


def _extract_json(text: str) -> dict | None:
    """Pull the plan object out of the CLI output.

    Tolerates: raw JSON, a ```json fence, and the `--output-format json` envelope
    ({"result": "<the model's JSON string>", ...}) — recursing into `result`."""
    text = text.strip()
    if text.startswith("```"):
        inner = text.split("```", 2)
        text = inner[1] if len(inner) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    obj: object
    try:
        obj = json.loads(text)
    except Exception:
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            obj = json.loads(text[i:j + 1])
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None
    if "targets" in obj or "plan" in obj:
        return obj
    result = obj.get("result")
    if isinstance(result, str):
        return _extract_json(result)
    return None


def parse_plan_response(raw: str, repo_rels: set[str]) -> PlanResult:
    """Parse the CLI output into a PlanResult, keeping only targets that name a
    real repo file (the model can hallucinate paths)."""
    obj = _extract_json(raw)
    if obj is None:
        return PlanResult(error="no JSON object in planning response")
    t_list = obj.get("targets")
    raw_targets = t_list if isinstance(t_list, list) else []
    targets: list[str] = []
    for t in raw_targets:
        if isinstance(t, str):
            n = _norm(t)
            if n in repo_rels and n not in targets:
                targets.append(n)
        if len(targets) >= MAX_TARGETS:
            break
    plan = str(obj.get("plan", "")).strip()
    return PlanResult(targets=targets, plan=plan)


def _host_login() -> bytes | None:
    """The host CLI's subscription login, as the substrate seeds it into the
    worker's redirected config. None when the auth comes from the environment
    (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, which the redirect keeps), when
    there is no login, or when the substrate cannot be asked. Never raises."""
    try:
        from shepherd_dialect.providers import _resolve_claude_auth_diagnostic

        return _resolve_claude_auth_diagnostic().blob
    except Exception:
        return None


def plan_targets(
    feature: str,
    file_tree_text: str,
    repo_rels: set[str],
    *,
    model: str = DEFAULT_PLAN_MODEL,
    timeout: int = PLAN_TIMEOUT,
    cli: str = "claude",
    repo: Path | str | None = None,
) -> PlanResult:
    """Run the cheap-model planning pass. Never raises — returns a PlanResult
    whose `error` is set (and targets/plan empty) on any failure. With `repo`
    the planner runs there and may read its files; without it, it runs in an
    empty directory and plans from the file tree alone."""
    binary = programs.which(cli)
    if not binary:
        return PlanResult(error=f"{cli} CLI not found on PATH")
    prompt = build_plan_prompt(feature, file_tree_text)
    try:
        # Started from the repository under review on the user's config, the
        # CLI followed the repository's CLAUDE.md, ran the hooks in its
        # .claude/settings.json and started the servers in its .mcp.json, in
        # this unjailed process; the flags keep those out. The user's config
        # is redirected too: `--setting-sources user` still loads the user's
        # own CLAUDE.md and settings, and a personal CLAUDE.md was measured
        # dictating the plan. The worker runs on a redirected config for the
        # same reasons; this is that redirect.
        with tempfile.TemporaryDirectory(prefix="shepherd-plan-") as scratch:
            root = Path(scratch)
            for sub in ("work", "home", "config", "tmp"):
                (root / sub).mkdir()
            login = _host_login()
            if login is not None:
                cred = root / "config" / ".credentials.json"
                cred.write_bytes(login)
                cred.chmod(0o600)
            env = {
                # the user's environment, kept before a repo's jail_env applied:
                # this CLI runs on the host, and NODE_OPTIONS or PATH set there
                # would choose what it loads
                **programs.host_env(),
                "HOME": str(root / "home"),
                "CLAUDE_CONFIG_DIR": str(root / "config"),
                "TMPDIR": str(root / "tmp"),
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
            proc = subprocess.run(
                [binary, "-p", prompt, "--output-format", "json", "--model", model, *PLAN_ISOLATION],
                capture_output=True, text=True, timeout=timeout, env=env,
                cwd=Path(repo) if repo is not None else root / "work",
            )
    except subprocess.TimeoutExpired:
        return PlanResult(error=f"planning timed out after {timeout}s")
    except OSError as exc:
        return PlanResult(error=f"planning could not run: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-200:]
        return PlanResult(error=f"planning exit {proc.returncode}: {detail}")
    return parse_plan_response(proc.stdout, repo_rels)
