"""Planning prefetch (#4): a cheap-model pass that decomposes the feature into a
short plan + the exact target files, fed to the worker so it skips the "where do
I even touch" exploration (the dominant wall-clock cost).

Runs the `claude` CLI in the PARENT process — outside the sandbox jail, where
network is allowed, exactly like the gate. Best-effort by design: any failure
(no CLI, timeout, bad JSON, unknown paths) yields an empty plan and the run
proceeds on keyword-scored targets exactly as before. No new dependency: it
reuses the `claude` CLI the worker already requires.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PLAN_MODEL = "claude-haiku-4-5-20251001"
PLAN_TIMEOUT = 60          # seconds for the planning call
MAX_TARGETS = 8            # cap on planned target files


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


def plan_targets(
    feature: str,
    file_tree_text: str,
    repo_rels: set[str],
    *,
    model: str = DEFAULT_PLAN_MODEL,
    timeout: int = PLAN_TIMEOUT,
    cli: str = "claude",
) -> PlanResult:
    """Run the cheap-model planning pass. Never raises — returns a PlanResult
    whose `error` is set (and targets/plan empty) on any failure."""
    binary = shutil.which(cli)
    if not binary:
        return PlanResult(error=f"{cli} CLI not found on PATH")
    prompt = build_plan_prompt(feature, file_tree_text)
    try:
        proc = subprocess.run(
            [binary, "-p", "--output-format", "json", "--model", model, prompt],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return PlanResult(error=f"planning timed out after {timeout}s")
    except OSError as exc:
        return PlanResult(error=f"planning could not run: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-200:]
        return PlanResult(error=f"planning exit {proc.returncode}: {detail}")
    return parse_plan_response(proc.stdout, repo_rels)
