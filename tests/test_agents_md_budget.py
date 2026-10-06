"""The repo's own AGENTS.md fits the worker's instruction budget.

`workspace_instructions` hands the worker the first INSTRUCTIONS_BUDGET
characters of AGENTS.md and cuts the rest. AGENTS.md promises that what the
worker needs (invariants, commands, language) sits before that cut; an edit
that grows the top of the file would break the promise silently, because the
pack never errors on a truncated instruction file. This pins the promise to
the real file, not a fixture.

The headroom past the last needle is a few dozen characters on purpose: the
file is meant to fill the budget. Only the file's content moves the cut: both
the pack and this test read it through `read_text`, whose newline translation
makes a CRLF checkout measure the same as an LF one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.contextpack import (  # noqa: E402
    INSTRUCTIONS_BUDGET,
    TRUNCATION_MARKER,
    workspace_instructions,
)

REPO = Path(__file__).resolve().parents[1]


class TestRepoAgentsMdFitsBudget(unittest.TestCase):
    def test_budget_matches_what_agents_md_states(self):
        # AGENTS.md's preamble quotes the budget; a change to either side fails here.
        doc = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(f"`INSTRUCTIONS_BUDGET` ({INSTRUCTIONS_BUDGET:,})", doc)

    def test_worker_facing_sections_land_before_the_cut(self):
        text = workspace_instructions(REPO)
        self.assertTrue(text.startswith("--- AGENTS.md ---"), text[:60])
        # The file is longer than the budget, so the cut must be real: without
        # the marker, `visible` would be the whole file and every needle below
        # would be found vacuously.
        self.assertIn(TRUNCATION_MARKER, text)
        visible = text.split(TRUNCATION_MARKER)[0]
        # The budget governs the file's content; the pack's own section header
        # ("--- AGENTS.md ---") sits outside it.
        content = visible.split("\n", 1)[1]
        self.assertLessEqual(len(content), INSTRUCTIONS_BUDGET)
        # The cut must land in prose: a fence left open hands the worker a
        # code block that swallows the marker and whatever the pack adds next.
        self.assertEqual(visible.count("```") % 2, 0, "the cut fell inside a code fence")
        # Same for inline spans: an odd backtick count means the prefix ends
        # inside one (Pullfrog caught `` (`claud `` + marker on PR #8).
        self.assertEqual(visible.count("`") % 2, 0, "the cut fell inside an inline code span")
        for needle in (
            "## Invariants",
            "**Generic solutions only.**",
            "**Settlement is human-only.**",
            "## Commands",
            "python -m unittest discover -s tests -v",
            "## Language and style",
            "not the file list.",  # the last sentence of that section, so the whole section is in
        ):
            self.assertIn(needle, visible, f"{needle!r} fell past the worker's budget")


if __name__ == "__main__":
    unittest.main()
