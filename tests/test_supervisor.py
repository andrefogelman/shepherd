"""Tests for #3c: the supervisor feeds each retry the worker's OWN prior proposal
so it iterates instead of restarting from scratch.

A faithful-but-minimal fake workspace records the guidance handed to each attempt;
the local gate is forced to fail (test_cmd="false"), so attempt 2's guidance must
carry attempt 1's proposed files. Runnable with:
    python -m unittest tests.test_supervisor
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.supervisor import _prior_attempt_guidance, develop  # noqa: E402


class PriorGuidanceHelper(unittest.TestCase):
    def test_empty_entries_yield_empty(self):
        self.assertEqual(_prior_attempt_guidance({}), "")

    def test_renders_files_and_caps(self):
        out = _prior_attempt_guidance({"a.py": b"X = 1\n"})
        self.assertIn("PREVIOUS ATTEMPT", out)
        self.assertIn("--- a.py ---", out)
        self.assertIn("X = 1", out)
        big = _prior_attempt_guidance({"b.py": b"y" * 20_000}, limit=500)
        self.assertLess(len(big), 900)
        self.assertIn("truncated", big)


# --- minimal fake workspace ---------------------------------------------------
class _Changeset:
    def __init__(self, files: dict[str, bytes]):
        self._files = files

    @property
    def changed_paths(self):
        return list(self._files)

    def read_file(self, rel):
        b = self._files.get(rel)
        return (b, 0o644) if b is not None else None


class _Output:
    def __init__(self, cs):
        self._cs = cs

    def changeset(self):
        return self._cs

    def discard(self):
        pass


class _Run:
    def __init__(self, ref, cs):
        self.run_ref = ref
        self._out = _Output(cs)

    def output(self):
        return self._out


class _Tasks:
    def register(self, task):
        pass


class _Workspace:
    """Returns a canned proposal per attempt and records each attempt's args."""

    def __init__(self, proposals: list[dict[str, bytes]]):
        self._proposals = proposals
        self._i = 0
        self.seen_guidance: list[str] = []
        self.tasks = _Tasks()

    def run(self, task, *, placement=None, runtime=None, **args):
        self.seen_guidance.append(args.get("guidance", ""))
        cs = _Changeset(self._proposals[min(self._i, len(self._proposals) - 1)])
        self._i += 1
        return _Run(f"r{self._i}", cs)

    def git_repo(self):
        return None


class RetryCarriesPriorDiff(unittest.TestCase):
    def test_attempt2_guidance_has_attempt1_proposal(self):
        repo_root = Path(tempfile.mkdtemp())
        (repo_root / "seed.txt").write_text("seed\n")
        ws = _Workspace([
            {"impl.py": b"def f():\n    return 'ATTEMPT_ONE_MARKER'\n"},
            {"impl.py": b"def f():\n    return 'ATTEMPT_TWO'\n"},
        ])
        report = develop(
            ws, task=object(), repo="r", repo_root=repo_root, feature="thing",
            test_cmd="false",  # local gate always fails -> forces the retry
            max_attempts=2, gate_timeout=30,
        )
        self.assertFalse(report.succeeded)
        self.assertEqual(len(ws.seen_guidance), 2)
        # attempt 1 saw no prior proposal; attempt 2 must carry attempt 1's file
        self.assertNotIn("ATTEMPT_ONE_MARKER", ws.seen_guidance[0])
        self.assertIn("ATTEMPT_ONE_MARKER", ws.seen_guidance[1])
        self.assertIn("PREVIOUS ATTEMPT", ws.seen_guidance[1])


if __name__ == "__main__":
    unittest.main()
