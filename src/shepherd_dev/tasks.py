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

import json
import os
from pathlib import Path

# ── Prompt registry (MUST stay inlined) ─────────────────────────────────────
# Duplicated from prompts.py ON PURPOSE: shepherd-ai 0.3.0 single-file capture
# rejects ANY same-package import (relative or absolute) in a task source at
# workspace.tasks.register(). Importing `.prompts` here crashes every claude
# provider run before the worker starts. Keep this block in sync with
# prompts.py (which remains the import target for non-task-source callers).

PROMPT_KEYS = (
    "implement", "write_tests", "review", "guidance_policy", "guidance_gate", "guidance_review",
)

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
    - `context`, when present, is a context pack computed from this very
      checkout: the file tree, the files most relevant to the feature (whole
      when small, signatures when large), notes learned from earlier runs,
      and the repository's own instructions for agents. Trust it and start
      from it; open additional files only when something you need is
      missing from it.
    - `guidance`, when present, is feedback from a previous failed attempt
      (test failures or policy violations). Fix the root cause it
      describes; do not repeat the same mistake.
    - `gate`, when present, is the exact command the supervisor will run
      to judge your proposal. It must pass. If that toolchain is usable
      here, run it yourself before you finish; if it is not, do not spend
      turns discovering that — write the change and stop.
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
    - `context`, when present, is a context pack computed from this very
      checkout (file tree, relevant files, notes from earlier runs, the
      repository's own agent instructions). Trust it and start from it;
      open additional files only when something you need is missing.
    - `guidance`, when present, is feedback from a previous failed
      attempt. Fix the root cause it describes.
    - `gate`, when present, is the exact command the supervisor will run
      to judge your proposal. The tests you write must pass under it.
    """,
    "review": """Review a proposed change to this repository.

    The repository contains the CURRENT (pre-change) code; read whatever
    you need for context. `context`, when non-empty, is a pre-computed
    context pack (file tree, relevant files, repo memory) — trust it and
    open additional files only if something you need is missing. `diff`
    contains the full proposed change (per-file new contents and
    deletions) for the feature described in `feature`. The proposal is
    NOT applied to the files you see.

    `diff` opens with `=== CHANGED FILES (n) ===` listing EVERY path the
    proposal touches, then a body per file — a unified diff where the file
    already existed, its full content where it is new. That list is the
    authoritative scope: if a body says some of it was `not shown`, you are
    judging that file on partial evidence and must NOT approve it on that
    basis. Say plainly in `summary` which files you could not fully read,
    and put the gap in `issues`.

    A path in that list which you cannot find in the repository is a NEW
    file the proposal creates — the tree you are reading is the state
    BEFORE this change. Never report that it "does not exist", is
    "missing", or was not written: searching a pre-change tree cannot tell
    you what the proposal adds, and `diff` already gave you its content.
    Not finding something is not evidence of its absence, and never a
    reason to reject.

    Assess: correctness, hidden bugs, security issues, scope discipline
    (does it touch only what the feature needs?), convention adherence,
    and missing edge cases. Be a rigorous skeptic; do not rubber-stamp.

    `lens`, when non-empty, narrows you to ONE of those dimensions and
    names it. Obey it literally: report only defects of that kind, and
    approve when you find none of that kind, even if something else about
    the change bothers you — a reviewer with a different lens is looking
    at the change at the same time and owns what you are leaving alone.
    When `lens` is empty you own all of the dimensions above, which is the
    ordinary single-reviewer case.

    `findings`, when non-empty, lists what an EARLIER round of this same
    review raised and that is still open, one per line as `- [id] text`.
    Judge each again against this proposal — they are not yours to take on
    trust — and then say which is which:
      - still present: re-raise it in `issues` with its id in leading
        square brackets, e.g. "[a1b2c3d4e5f6] what is still wrong". Do
        NOT describe it afresh in your own words instead; that reads as a
        different problem and hides that this one came back.
      - genuinely gone: put its id in `resolved`.
    A listed finding you put in neither stays open, which is the right
    outcome when you did not check it.

    `proposed_root` is a directory holding the proposal ALREADY APPLIED —
    every changed file, at its repository-relative path, with the content
    this change would give it. Read it whenever you need the result rather
    than the change: to parse a file, compile it, count its lines, or check
    a function is closed. `diff` shows you what moved; `proposed_root` is
    what the worker actually produced.

    Never rebuild the result yourself by applying `diff` by hand. Your
    reconstruction is not the deliverable, and a mistake in it — treating a
    context line as removed is the usual one — becomes a defect you report
    against code that does not have it. If you ran a parser or a compiler,
    say in `summary` which file you ran it on.

    `gate`, when present, names the command the supervisor already ran
    against this exact proposal and how it ended. A gate that passed has
    compiled and tested the change; weigh any claim that it cannot build
    or fails its tests against that fact before you make it.

    If you need to write something to do the work — reconstructing a file
    to check it parses, saving a diff to apply — put it under
    `.review-scratch/` at the repository root, and nowhere else. That
    directory is yours and is ignored when your verdict is checked. A file
    you write anywhere else invalidates the verdict, including a file you
    delete afterwards.

    Write EXACTLY ONE file outside it: `REVIEW.json` at the repository root,
    valid JSON with this schema and nothing else. Do not modify any other
    file; any other change invalidates your verdict.
    {
      "approved": true | false,
      "summary": "<one-paragraph overall assessment>",
      "issues": ["<specific issue with file/line when possible>", ...],
      "resolved": ["<id of a listed finding this change actually removes>", ...]
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
    "guidance_review": (
        "PREVIOUS ATTEMPT: passed the test suite but the reviewer REJECTED it.\n"
        "These findings are still open and each one is tracked by id:\n{FINDINGS}\n"
        "Fix every one of them. Restating a finding in milder words or lowering its "
        "severity does not close it — only a change that removes the problem does. "
        "If one genuinely cannot be fixed within the scope you were given, leave it "
        "alone and say so plainly in your response; do not weaken or delete existing "
        "tests to get past it."
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


def implement(repo: sp.GitRepo, feature: str, guidance: str = "", context: str = "", gate: str = "") -> None: ...
implement.__doc__ = get_prompt("implement")
implement = sp.task(implement)


def write_tests(repo: sp.GitRepo, feature: str, guidance: str = "", context: str = "", gate: str = "") -> None: ...
write_tests.__doc__ = get_prompt("write_tests")
write_tests = sp.task(write_tests)


def review(repo: sp.GitRepo, feature: str, diff: str, context: str = "", findings: str = "", lens: str = "", proposed_root: str = "", gate: str = "") -> None: ...
review.__doc__ = get_prompt("review")
review = sp.task(review)


@sp.task
def smoke_change(repo: sp.GitRepo, output_path: str, output_text: str) -> None:
    """Write `output_text` to `output_path` inside the retained output.

    Deterministic task used only to smoke-test the supervisor loop with
    the offline `static` provider. Not part of the real development flow.
    """
