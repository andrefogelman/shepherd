"""What jail_env and jail_seed actually reach — and what they do not.

Measured from a real run's journal (20260808-161228-bbd282, worker under the
macOS Seatbelt jail), where the worker probed its own permissions:

    ls  /Users/andrefogelman/.cache/sac-build   → listed it (READ works)
    touch .../sac-build/probe                   → Operation not permitted
    mkdir /tmp/probe-write-test                 → Operation not permitted

So a jailed worker READS outside its clone and WRITES nowhere but inside it.
jail_env pointing at a dependency cache therefore works for the worker;
jail_seed pointing at a build root does not — the worker cannot write to the
seeded copy, and `mix format`, which needs compiled plugins, dies before
formatting anything.

The manual claimed both features let the worker compile. Half of that was
wrong, and wrong in the direction that wastes a whole attempt: the worker
spent ten minutes hunting a permission error instead of doing the task.

These tests pin the boundary so the documentation cannot drift back.

Runnable with: python -m unittest tests.test_jail_reach
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

MANUALS = ("docs/MANUAL.en.md", "docs/MANUAL.md")


def _manual(name: str) -> str:
    return (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")


class SeededPathIsOutsideTheCloneTests(unittest.TestCase):
    """The mechanical fact the documentation has to describe."""

    def test_the_seeded_copy_lives_outside_any_clone(self):
        import json
        import os

        from tmpdirs import mkdtemp

        from shepherd_dev.config import jail_seed_applied

        repo = Path(mkdtemp(prefix="shepherd-reach-"))
        origin = Path(mkdtemp(prefix="shepherd-reach-origin-"))
        (origin / "a").write_text("x")
        (repo / ".shepherd-dev.json").write_text(
            json.dumps({"jail_seed": {"BUILD": str(origin)}}), encoding="utf-8"
        )
        with jail_seed_applied(repo):
            seeded = Path(os.environ["BUILD"])
            self.assertFalse(
                seeded.is_relative_to(repo),
                "the seed is a temp dir, not something a jailed worker can write",
            )


class ManualStatesTheBoundaryTests(unittest.TestCase):
    def test_both_manuals_say_the_worker_cannot_write_outside_its_clone(self):
        for name in MANUALS:
            with self.subTest(manual=name):
                text = " ".join(_manual(name).split())
                self.assertIn("Operation not permitted", text)

    def test_both_manuals_say_jail_seed_serves_the_gate_not_the_worker(self):
        for name in MANUALS:
            with self.subTest(manual=name):
                text = " ".join(_manual(name).split()).lower()
                self.assertIn("jail_seed", text)
                self.assertTrue(
                    "not the worker" in text or "não o worker" in text,
                    "the limit has to be stated, not implied",
                )

    def test_neither_manual_still_promises_the_worker_can_compile(self):
        """The sentence that sent a reader down a ten-minute dead end."""
        for name in MANUALS:
            with self.subTest(manual=name):
                text = " ".join(_manual(name).split())
                for claim in (
                    "so the toolchain can reach a cache that lives outside the clone",
                    "para o toolchain alcançar um cache que vive fora do clone",
                ):
                    self.assertNotIn(claim, text)

    def test_both_manuals_point_at_pre_gate_cmd_for_formatting(self):
        """Formatting is the case the worker was being asked to handle; it has
        a home that works."""
        for name in MANUALS:
            with self.subTest(manual=name):
                self.assertIn("pre_gate_cmd", _manual(name))


if __name__ == "__main__":
    unittest.main()
