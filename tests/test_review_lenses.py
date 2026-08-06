"""Tests for the lens-differentiated review panel. Runnable with:
python -m unittest tests.test_review_lenses
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class LensCatalogueTests(unittest.TestCase):
    def test_the_five_dimensions_the_review_prompt_already_names(self):
        from shepherd_dev.prompts import LENS_NAMES

        self.assertEqual(
            LENS_NAMES,
            ("correctness", "security", "scope", "conventions", "tests"),
        )

    def test_every_name_has_instruction_text(self):
        from shepherd_dev.prompts import LENS_NAMES, REVIEW_LENSES

        self.assertEqual(tuple(REVIEW_LENSES), LENS_NAMES)
        for name, text in REVIEW_LENSES.items():
            with self.subTest(lens=name):
                self.assertTrue(text.strip(), f"{name} has no instruction")
                self.assertGreater(len(text), 80, f"{name}'s text is too thin to steer a reviewer")

    def test_each_lens_tells_the_reviewer_to_stay_in_its_lane(self):
        """A lens that re-audits everything is just the generic reviewer
        again, and the panel goes back to K correlated samples."""
        from shepherd_dev.prompts import REVIEW_LENSES

        for name, text in REVIEW_LENSES.items():
            with self.subTest(lens=name):
                self.assertIn("only", text.lower())

    def test_the_review_prompt_explains_the_lens_argument(self):
        from shepherd_dev.prompts import get_prompt

        prompt = get_prompt("review")
        self.assertIn("`lens`", prompt)
        # and it must say what an EMPTY lens means, since that is the default
        self.assertIn("empty", prompt.lower())

    def test_the_catalogue_is_not_in_the_optimizer_editable_set(self):
        """PROMPT_KEYS/EDITABLE_KEYS are the tunable core prompts. The lens
        catalogue is a taxonomy — letting the optimizer rewrite a lens would
        quietly change what that reviewer is even responsible for."""
        from shepherd_dev.optimize import EDITABLE_KEYS
        from shepherd_dev.prompts import LENS_NAMES, PROMPT_KEYS

        for name in LENS_NAMES:
            self.assertNotIn(name, PROMPT_KEYS)
            self.assertNotIn(name, EDITABLE_KEYS)


class ReviewTaskSignatureTests(unittest.TestCase):
    def test_the_review_task_accepts_a_lens_and_defaults_it_empty(self):
        import inspect

        from shepherd_dev import tasks

        fn = getattr(tasks.review, "__wrapped__", None) or tasks.review
        params = inspect.signature(fn).parameters
        self.assertIn("lens", params)
        self.assertEqual(params["lens"].default, "")


if __name__ == "__main__":
    unittest.main()
