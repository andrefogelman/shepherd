"""Render the worker's and reviewer's prompts ourselves — pure stdlib.

What the model received before this module existed was the substrate's
workspace-control envelope (`_claude_runtime_prompt`): the ENTIRE source of
tasks.py as a fenced "task contract" (every prompt of every task, for every
run), then the run's arguments as one `json.dumps(indent=2)` block — with
`ensure_ascii` on, so a 25k-char context pack and a 5k-char feature arrived as
single-line strings full of `\\n` and every accented character spelled
`\\u00e7`. The task's own docstring, the actual instructions, sat inside that
Python block as a string literal. Measured on 182 worker attempts: the first
tool call was `pwd && ls -la` in most of them and the median attempt spent
18 tool calls exploring before its first edit, pack or no pack — the pack was
there, but not in a shape a model reads.

This module renders the same inputs as a document: the task's prompt first,
then one titled section per argument, long values fenced so nothing inside
them (a `## ` line in a copied CLAUDE.md, say) can pass for a section of the
prompt itself. It knows only the three shepherd-dev tasks; anything else keeps
the substrate's envelope untouched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .prompts import DEFAULT_PROMPTS, get_prompt

#: The task ids the substrate derives for our tasks (module.qualname) map to
#: prompt keys; the tuple is the order the sections are laid out in. Long,
#: reference-like material comes first and the thing to act on last, so the
#: instruction the model must obey is the freshest thing it read.
TASK_SECTIONS: dict[str, tuple[str, ...]] = {
    "implement": ("context", "feature", "guidance", "gate"),
    "write_tests": ("context", "feature", "guidance", "gate"),
    "review": ("context", "diff", "feature", "findings", "lens", "proposed_root", "gate"),
}

_TASK_MODULE = "shepherd_dev.tasks."

#: One line per task telling the model what to do now that it has read
#: everything. The docstrings carry the rules; this is the cue to start.
CLOSING: dict[str, str] = {
    "implement": (
        "The current working directory is the repository. Begin now: read what the "
        "context leaves unclear, write the files, and stop when the change is complete."
    ),
    "write_tests": (
        "The current working directory is the repository. Begin now: write the tests "
        "as files under the repository and stop when they are complete."
    ),
    "review": (
        "The current working directory is the repository WITH the proposal applied: "
        "every changed file is at its path with its proposed content. Begin the review "
        "now and finish by writing REVIEW.json at the repository root."
    ),
}


def task_key(task_id: str) -> str | None:
    """`shepherd_dev.tasks.implement` -> `implement`; anything else -> None."""
    if not isinstance(task_id, str) or not task_id.startswith(_TASK_MODULE):
        return None
    key = task_id[len(_TASK_MODULE):]
    return key if key in TASK_SECTIONS and key in DEFAULT_PROMPTS else None


def fence(text: str) -> str:
    """A backtick fence one tick longer than any run inside `text`, so the
    value can never close its own fence early."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _dedent_prompt(text: str) -> str:
    """Docstrings are indented by their position in tasks.py; the model should
    not see four leading spaces on every line."""
    import textwrap

    lines = text.strip("\n").splitlines()
    if not lines:
        return ""
    head, rest = lines[0], "\n".join(lines[1:])
    return (head.strip() + "\n" + textwrap.dedent(rest)).strip()


def render_section(name: str, value: object) -> str | None:
    """One `## name` section, or None when there is nothing to say.

    A single-line value stays inline under its heading; a multi-line one is
    fenced, so its own lines cannot read as headings, list items or code of
    this document."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip("\n")
    if not text.strip():
        return None
    if "\n" not in text:
        return f"## {name}\n{text}"
    f = fence(text)
    return f"## {name}\n{f}text\n{text}\n{f}"


def render_prompt(task_id: str, kwargs: Mapping[str, object], *, fallback: str) -> str:
    """The prompt for one of our tasks, or `fallback` for any other task.

    `fallback` is what the substrate would have sent; it is returned unchanged
    for a task this module does not know (the static smoke task, a task from
    another package), so nothing that is not ours changes shape.
    """
    key = task_key(task_id)
    if key is None:
        return fallback
    parts = [_dedent_prompt(get_prompt(key))]
    for name in TASK_SECTIONS[key]:
        section = render_section(name, kwargs.get(name))
        if section is not None:
            parts.append(section)
    parts.append(CLOSING[key])
    return "\n\n".join(parts) + "\n"


def prompt_summary(task_id: str, kwargs: Mapping[str, object], rendered: str) -> dict:
    """What the event log records about a prompt: shape, never content."""
    key = task_key(task_id)
    present = []
    if key is not None:
        present = [
            name for name in TASK_SECTIONS[key]
            if isinstance(kwargs.get(name), str) and kwargs.get(name).strip()  # type: ignore[union-attr]
        ]
    return {
        "task": key or task_id,
        "rendered": key is not None,
        "chars": len(rendered),
        "sections": present,
    }
