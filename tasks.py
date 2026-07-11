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
def smoke_change(repo: sp.GitRepo, output_path: str, output_text: str) -> None:
    """Write `output_text` to `output_path` inside the retained output.

    Deterministic task used only to smoke-test the supervisor loop with
    the offline `static` provider. Not part of the real development flow.
    """
