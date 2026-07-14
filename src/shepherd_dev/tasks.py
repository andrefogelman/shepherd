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

import shepherd as sp

from .prompts import (  # noqa: F401 — re-exported for back-compat
    DEFAULT_PROMPTS,
    OVERRIDES_FILE,
    PROMPT_KEYS,
    _file_overrides,
    get_prompt,
    load_overrides,
    save_overrides,
)


def implement(repo: sp.GitRepo, feature: str, guidance: str = "") -> None: ...
implement.__doc__ = get_prompt("implement")
implement = sp.task(implement)


def write_tests(repo: sp.GitRepo, feature: str, guidance: str = "") -> None: ...
write_tests.__doc__ = get_prompt("write_tests")
write_tests = sp.task(write_tests)


def review(repo: sp.GitRepo, feature: str, diff: str, context: str = "") -> None: ...
review.__doc__ = get_prompt("review")
review = sp.task(review)


@sp.task
def smoke_change(repo: sp.GitRepo, output_path: str, output_text: str) -> None:
    """Write `output_text` to `output_path` inside the retained output.

    Deterministic task used only to smoke-test the supervisor loop with
    the offline `static` provider. Not part of the real development flow.
    """
