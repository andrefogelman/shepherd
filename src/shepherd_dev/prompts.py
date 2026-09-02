"""Prompt registry + override resolution — pure stdlib, NO shepherd-ai import.

Split out of tasks.py (#10) so callers that only need a prompt string (e.g. the
supervisor's guidance formatting) don't drag in the shepherd runtime. tasks.py
carries an INLINED COPY of these names (shepherd-ai 0.3.0 single-file capture
forbids a task source importing same-package modules) — keep both in sync.

Resolution: DEFAULT_PROMPTS < overrides file (~/.shepherd-dev/prompts-overrides.json,
override via SHEPHERD_DEV_PROMPTS_OVERRIDES).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PROMPT_KEYS = (
    "implement", "write_tests", "review", "guidance_policy", "guidance_gate", "guidance_review",
)

OVERRIDES_FILE = Path(
    os.environ.get("SHEPHERD_DEV_PROMPTS_OVERRIDES")
    or Path.home() / ".shepherd-dev" / "prompts-overrides.json"
)

#: One reviewer per dimension, instead of K reviewers sharing one prompt.
#:
#: These are not a new taxonomy: they are the dimensions the generic review
#: prompt already lists in a single sentence ("correctness, hidden bugs,
#: security issues, scope discipline, convention adherence, and missing edge
#: cases"). Asking one reviewer to weigh all of them at once is what makes K
#: samples of that prompt correlate — they share its blind spot. Splitting
#: the sentence into separate reviewers is the whole feature.
#:
#: Each text ends by confining the reviewer to its lane. Without that, every
#: lens drifts back into a general audit and the panel is K correlated
#: samples again, only more expensive.
REVIEW_LENSES: dict[str, str] = {
    "correctness": (
        "You are the CORRECTNESS reviewer. Ask only whether this change does "
        "what it claims: wrong logic, off-by-one, an unhandled None, a branch "
        "that cannot be reached, state mutated where a copy was meant, an "
        "error swallowed, a race between two things that look sequential. "
        "Trace the actual values through the changed code rather than reading "
        "it for plausibility. Report ONLY correctness defects — another "
        "reviewer owns security, scope, conventions and tests."
    ),
    "security": (
        "You are the SECURITY reviewer. Ask only what an attacker or a "
        "hostile input could do with this change: injection through an "
        "unescaped value, a secret or token reaching a log or an error "
        "message, a path that escapes its root, a permission or ownership "
        "check that is missing or can be skipped, an unsafe default, a "
        "dependency or subprocess invoked with attacker-influenced "
        "arguments. Report ONLY security defects — another reviewer owns "
        "correctness, scope, conventions and tests."
    ),
    "scope": (
        "You are the SCOPE reviewer. Ask only whether this change is the "
        "change that was asked for: files touched that the feature does not "
        "need, refactoring smuggled in beside the fix, a behavior altered "
        "for callers who did not ask, a public signature or output format "
        "changed without cause, dead code or debugging residue left behind. "
        "Judge against the stated feature, not against your taste. Report "
        "ONLY scope defects — another reviewer owns correctness, security, "
        "conventions and tests."
    ),
    "conventions": (
        "You are the CONVENTIONS reviewer. Ask only whether this change "
        "reads like the code around it: naming, structure and error "
        "handling that match the surrounding module, an existing helper "
        "reimplemented instead of reused, a comment that states what the "
        "code plainly says rather than why it is that way, a comment that "
        "no longer matches the code beneath it. Compare against the actual "
        "neighbouring files, not a general style guide. Report ONLY "
        "convention defects — another reviewer owns correctness, security, "
        "scope and tests."
    ),
    "tests": (
        "You are the TEST reviewer. Ask only whether this change is "
        "genuinely covered: a new behavior with no test, a test that asserts "
        "nothing or would pass against the unfixed code, a mock standing in "
        "for the very thing under test, an edge case named in the code but "
        "absent from the tests, a test whose name promises more than it "
        "checks. A bug fix with no test that fails without it is the "
        "clearest case. Report ONLY test defects — another reviewer owns "
        "correctness, security, scope and conventions."
    ),
}

#: Catalogue order — what `--review-lens` accepts, and the order a panel runs.
LENS_NAMES: tuple[str, ...] = tuple(REVIEW_LENSES)

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

    The working directory holds the repository WITH the proposal applied:
    every file the proposal touches is at its path with its proposed
    content, and everything else is as it was. Read whatever you need for
    context. `context`, when non-empty, is a pre-computed context pack
    (file tree, relevant files, repo memory) — trust it and open additional
    files only if something you need is missing. `diff` is the change
    itself for the feature described in `feature`: its `-` lines are what
    the pre-change file said, its `+` lines what the working directory now
    says.

    `diff` opens with `=== CHANGED FILES (n) ===` listing EVERY path the
    proposal touches, then a body per file — a unified diff where the file
    already existed, its full content where it is new. That list is the
    authoritative scope: if a body says some of it was `not shown`, open the
    file in the working directory and read it there before judging it; a
    file you did not fully read is not one you may approve. Say plainly in
    `summary` which files you could not fully read, and put the gap in
    `issues`.

    Every path in that list exists in the working directory with its
    proposed content. Never report that one "does not exist", is "missing"
    or was not written without opening it there: not finding something in
    a search is not evidence of its absence, and never a reason to reject.

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

    `proposed_root` is a directory holding ONLY the changed files, at their
    repository-relative paths, with the content this change gives them — a
    second copy of what is already applied in the working directory, handy
    for listing exactly what the worker produced. Parse, compile or count
    in the working directory, where the changed files sit among the rest.

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

    Every finding carries a severity, and the severity decides the verdict:
      - "blocking": you would hold the change back for it — wrong behavior,
        a security hole, a build or test that breaks, scope the request did
        not ask for, a contract changed for callers who did not ask.
      - "advisory": you would mention it and ship anyway — style, naming, a
        comment, an optional improvement, a test you would add. Say it, but
        it does not block.
    Do not inflate: a finding you would not hold the change back for is
    advisory, however much it bothers you. `approved` MUST be true when no
    finding is blocking, and false when any is — nothing else decides it.

    Write EXACTLY ONE file outside it: `REVIEW.json` at the repository root,
    valid JSON with this schema and nothing else. Do not modify any other
    file; any other change invalidates your verdict.
    {
      "approved": true | false,
      "summary": "<one-paragraph overall assessment>",
      "issues": [
        {"severity": "blocking" | "advisory",
         "text": "<specific finding with file/line when possible>"},
        ...
      ],
      "resolved": ["<id of a listed finding this change actually removes>", ...]
    }
    A re-raised earlier finding keeps its `[id]` at the start of its `text`.
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
