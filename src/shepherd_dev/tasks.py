"""Worker tasks + the prompt-asset store (CRO-lite optimization surface).

This module is a Shepherd task source, so the framework re-imports it in
isolation and REJECTS any same-package/relative import. Everything it needs
must therefore live here or in the standard library — hence prompts live in
this file, not a sibling module. Other modules (supervisor, optimize) import
`get_prompt` / `save_overrides` from here; that is fine (they are not task
sources).

Resolution: DEFAULT_PROMPTS < overrides file (~/.shepherd-dev/
prompts-overrides.json, override via SHEPHERD_DEV_PROMPTS_OVERRIDES). Worker
docstrings are set from get_prompt AT IMPORT, so a candidate prompt is
validated by running in a subprocess with the overrides env pointed at it —
required because the framework only accepts module-level task objects.

Guidance templates use literal {TOKENS} substituted with str.replace (never
str.format — gate tails may contain braces).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import shepherd as sp

PROMPT_KEYS = ("implement", "write_tests", "review", "guidance_policy", "guidance_gate")

OVERRIDES_FILE = Path(
    os.environ.get("SHEPHERD_DEV_PROMPTS_OVERRIDES")
    or Path.home() / ".shepherd-dev" / "prompts-overrides.json"
)

DEFAULT_PROMPTS: dict[str, str] = {
    "implement": """Implement the requested feature in the repository.

    Requirements:
    - Follow the existing conventions of the codebase (style, naming,
      structure, test framework). Read neighboring code before writing.
    - Touch only the files needed for this feature. Do not refactor,
      reformat, or "improve" unrelated code.
    - Keep the change minimal and complete: no TODOs, no placeholders,
      no dead code, no broken imports.
    - If `guidance` is non-empty, it contains feedback from a previous
      failed attempt (test failures or policy violations). Fix the root
      cause it describes; do not repeat the same mistake.
    - Write your changes as regular files in the repository. They will be
      held for human review before anything is applied.
    """,
    "write_tests": """Write or update automated tests for the described feature or behavior.

    Requirements:
    - Use the repository's existing test framework, layout, and naming
      conventions. Read existing tests before writing new ones.
    - Tests must verify INTENT (the business rule), not just current
      behavior: a test that keeps passing when the rule breaks is wrong.
    - New and updated tests must pass against the current code. Do not
      change production code; only test files.
    - If `guidance` is non-empty, it contains feedback from a previous
      failed attempt. Fix the root cause it describes.
    """,
    "review": """Review a proposed change to this repository.

    The repository contains the CURRENT (pre-change) code; read whatever
    you need for context. `diff` contains the full proposed change
    (per-file new contents and deletions) for the feature described in
    `feature`. The proposal is NOT applied to the files you see.

    Assess: correctness, hidden bugs, security issues, scope discipline
    (does it touch only what the feature needs?), convention adherence,
    and missing edge cases. Be a rigorous skeptic; do not rubber-stamp.

    Write EXACTLY ONE file: `REVIEW.json` at the repository root, valid
    JSON with this schema and nothing else. Do not modify any other file;
    any other change invalidates your verdict.
    {
      "approved": true | false,
      "summary": "<one-paragraph overall assessment>",
      "issues": ["<specific issue with file/line when possible>", ...]
    }
    An empty issues list is only acceptable with approved=true.
    """,
    "guidance_policy": (
        "PREVIOUS ATTEMPT: rejected by policy before testing.\n"
        "Violations:\n- {VIOLATIONS}\n"
        "Stay strictly within the feature's scope and try again."
    ),
    "guidance_gate": (
        "PREVIOUS ATTEMPT: failed the test suite (exit {EXIT}).\n"
        "Test output (tail):\n{TAIL}\n"
        "Diagnose the root cause shown above and fix it; do not just retry the same change."
    ),
}


def _file_overrides() -> dict[str, str]:
    try:
        data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k in PROMPT_KEYS and isinstance(v, str)}
    except Exception:
        return {}


def get_prompt(key: str) -> str:
    if key not in PROMPT_KEYS:
        raise KeyError(key)
    return _file_overrides().get(key, DEFAULT_PROMPTS[key])


def load_overrides() -> dict[str, str]:
    """Persisted overrides only (for `optimize` to diff against)."""
    return _file_overrides()


def save_overrides(overrides: dict[str, str]) -> None:
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    merged = {**_file_overrides(), **{k: v for k, v in overrides.items() if k in PROMPT_KEYS}}
    OVERRIDES_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def implement(repo: sp.GitRepo, feature: str, guidance: str = "") -> None: ...
implement.__doc__ = get_prompt("implement")
implement = sp.task(implement)


def write_tests(repo: sp.GitRepo, feature: str, guidance: str = "") -> None: ...
write_tests.__doc__ = get_prompt("write_tests")
write_tests = sp.task(write_tests)


def review(repo: sp.GitRepo, feature: str, diff: str) -> None: ...
review.__doc__ = get_prompt("review")
review = sp.task(review)


@sp.task
def smoke_change(repo: sp.GitRepo, output_path: str, output_text: str) -> None:
    """Write `output_text` to `output_path` inside the retained output.

    Deterministic task used only to smoke-test the supervisor loop with
    the offline `static` provider. Not part of the real development flow.
    """
