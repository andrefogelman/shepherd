"""Worker tasks: typed contracts fulfilled by a sandboxed agent.

The docstring of each task is the prompt contract; the signature is the
whole permission surface (grants come from the workspace binding).
"""

from __future__ import annotations

import shepherd as sp


@sp.task
def implement(repo: sp.GitRepo, feature: str, guidance: str = "") -> None:
    """Implement the requested feature in the repository.

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
    """


@sp.task
def write_tests(repo: sp.GitRepo, feature: str, guidance: str = "") -> None:
    """Write or update automated tests for the described feature or behavior.

    Requirements:
    - Use the repository's existing test framework, layout, and naming
      conventions. Read existing tests before writing new ones.
    - Tests must verify INTENT (the business rule), not just current
      behavior: a test that keeps passing when the rule breaks is wrong.
    - New and updated tests must pass against the current code. Do not
      change production code; only test files.
    - If `guidance` is non-empty, it contains feedback from a previous
      failed attempt. Fix the root cause it describes.
    """


@sp.task
def review(repo: sp.GitRepo, feature: str, diff: str) -> None:
    """Review a proposed change to this repository.

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
    """


@sp.task
def smoke_change(repo: sp.GitRepo, output_path: str, output_text: str) -> None:
    """Write `output_text` to `output_path` inside the retained output.

    Deterministic task used only to smoke-test the supervisor loop with
    the offline `static` provider. Not part of the real development flow.
    """
