"""The prompt store exists twice, and nothing was checking the copies matched.

shepherd-ai rejects any same-package import inside a task source, so tasks.py
cannot import prompts.py and carries an inlined copy of it instead. Two copies
with a comment asking humans to keep them in sync is not a mechanism, and they
had already drifted: `guidance_review` was added to prompts.py and to
optimize.EDITABLE_KEYS but not to tasks.py, and since optimize reads
DEFAULT_PROMPTS and save_overrides from tasks.py, `shepherd-dev optimize`
raised `KeyError: 'guidance_review'` on every invocation that reached the
proposal step, and an override for that key could never have been persisted.

These tests are the mechanism. They fail on the drift itself, not on a symptom.
Runnable with: python -m unittest tests.test_prompt_copies
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev import prompts as _prompts  # noqa: E402
from shepherd_dev import tasks as _tasks  # noqa: E402
from shepherd_dev.optimize import EDITABLE_KEYS  # noqa: E402


class TheTwoCopiesAgree(unittest.TestCase):
    def test_the_same_keys_exist_in_both(self):
        self.assertEqual(
            sorted(_tasks.DEFAULT_PROMPTS), sorted(_prompts.DEFAULT_PROMPTS)
        )

    def test_every_prompt_is_byte_identical(self):
        for key in sorted(_prompts.DEFAULT_PROMPTS):
            with self.subTest(prompt=key):
                self.assertEqual(
                    _tasks.DEFAULT_PROMPTS.get(key),
                    _prompts.DEFAULT_PROMPTS.get(key),
                    f"the inlined copy of {key!r} in tasks.py has drifted",
                )

    def test_the_key_tuples_agree(self):
        self.assertEqual(tuple(_tasks.PROMPT_KEYS), tuple(_prompts.PROMPT_KEYS))

    def test_every_declared_key_actually_has_a_prompt(self):
        for key in _prompts.PROMPT_KEYS:
            with self.subTest(prompt=key):
                self.assertIn(key, _prompts.DEFAULT_PROMPTS)
                self.assertIn(key, _tasks.DEFAULT_PROMPTS)


class OptimizeCanReachEveryKeyItOffers(unittest.TestCase):
    """optimize reads its prompts from tasks.py and writes back through it, so
    a key it advertises as editable but cannot read is a crash, and one it
    cannot write is a silent no-op."""

    def test_every_editable_key_is_readable(self):
        # The exact expression that raised: optimize._propose builds
        # {k: DEFAULT_PROMPTS[k] for k in EDITABLE_KEYS}.
        missing = [k for k in EDITABLE_KEYS if k not in _tasks.DEFAULT_PROMPTS]
        self.assertEqual(missing, [], "optimize would raise KeyError on these")

    def test_every_editable_key_survives_being_saved(self):
        # save_overrides filters by tasks.PROMPT_KEYS and drops the rest without
        # a word, so a winning candidate for a filtered key is thrown away
        # while the command reports success.
        dropped = [k for k in EDITABLE_KEYS if k not in _tasks.PROMPT_KEYS]
        self.assertEqual(dropped, [], "save_overrides would discard these silently")


if __name__ == "__main__":
    unittest.main()
