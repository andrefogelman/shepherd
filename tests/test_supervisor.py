"""Tests for #3c: the supervisor feeds each retry the worker's OWN prior proposal
so it iterates instead of restarting from scratch.

A faithful-but-minimal fake workspace records the guidance handed to each attempt;
the local gate is forced to fail (test_cmd="false"), so attempt 2's guidance must
carry attempt 1's proposed files. Runnable with:
    python -m unittest tests.test_supervisor
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev.diffcollect import Entries  # noqa: E402
from shepherd_dev.progress import ProgressReporter  # noqa: E402
from shepherd_dev.supervisor import (  # noqa: E402
    _prior_attempt_guidance,
    _render_entries_as_diff_text,
    build_diff_text,
    develop,
    materialize_into,
    read_changeset_entries,
)


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
    def __init__(self, files: dict[str, bytes], modes: dict[str, int] | None = None):
        self._files = files
        self._modes = modes or {}

    @property
    def changed_paths(self):
        return list(self._files)

    def read_file(self, rel):
        b = self._files.get(rel)
        return (b, self._modes.get(rel, 0o100644)) if b is not None else None


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
        repo_root = Path(mkdtemp())
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


class ProgressWiring(unittest.TestCase):
    def test_reporter_receives_phase_lines_and_activity(self):
        repo_root = Path(mkdtemp())
        (repo_root / "seed.txt").write_text("seed\n")
        ws = _Workspace([{"impl.py": b"def f():\n    return 1\n"}])
        buf = io.StringIO()
        develop(
            ws, task=object(), repo="r", repo_root=repo_root, feature="thing",
            test_cmd="false", max_attempts=2, gate_timeout=30,
            reporter=ProgressReporter(stream=buf, enabled=False),
        )
        out = buf.getvalue()
        self.assertIn("attempt 1/2 · worker running", out)
        self.assertIn("worker: 1 file(s): impl.py", out)   # post-hoc #B note
        # Every phase of one attempt carries the same label: adjacent lines
        # naming the same attempt with different numbers is how the counter
        # became unreadable in the first place.
        self.assertIn("attempt 1/2 · gate", out)
        self.assertIn("✗", out)                             # gate 'false' fails


class ExecBitSurvives(unittest.TestCase):
    """The changeset read carries git's filemode; the write must re-apply it.
    Without this, `chmod +x deploy.sh` inside a proposal lands as 0o644 in the
    gate tree and — if accepted — in the real repo after settle."""

    def test_read_changeset_entries_marks_git_executable_mode(self):
        cs = _Changeset(
            {"deploy.sh": b"#!/bin/sh\n", "a.py": b"a=1\n"},
            modes={"deploy.sh": 0o100755},
        )
        entries = read_changeset_entries(cs)
        self.assertIsInstance(entries, Entries)
        self.assertEqual(entries.executable, frozenset({"deploy.sh"}))

    def test_materialize_into_applies_the_exec_bit(self):
        root = Path(mkdtemp())
        entries = Entries(
            {"deploy.sh": b"#!/bin/sh\necho hi\n", "a.py": b"a=1\n"},
            executable={"deploy.sh"},
        )
        materialize_into(root, entries)
        self.assertTrue((root / "deploy.sh").stat().st_mode & 0o111)
        self.assertFalse((root / "a.py").stat().st_mode & 0o111)

    def test_plain_dict_writes_with_the_default_mode(self):
        """No exec information (any plain dict, e.g. built by hand in a test)
        means today's behavior: the filesystem default, no chmod."""
        root = Path(mkdtemp())
        materialize_into(root, {"run.sh": b"#!/bin/sh\n"})
        self.assertFalse((root / "run.sh").stat().st_mode & 0o111)


class RenderEntriesAsDiffTextTests(unittest.TestCase):
    def test_renders_each_file_with_a_header(self):
        text = _render_entries_as_diff_text({"a.py": b"A = 1\n", "b.py": b"B = 2\n"})
        self.assertIn("=== FILE: a.py (proposed content) ===", text)
        self.assertIn("A = 1", text)
        self.assertIn("=== FILE: b.py (proposed content) ===", text)
        self.assertIn("B = 2", text)

    def test_truncates_past_the_limit(self):
        text = _render_entries_as_diff_text({"big.py": b"x" * 100}, limit=20)
        self.assertLessEqual(len(text), 20 + len("\n\n[... truncated at 20 chars ...]"))
        self.assertIn("truncated at 20 chars", text)

    def test_build_diff_text_still_delegates_correctly(self):
        """build_diff_text takes a real changeset (with .changed_paths /
        .read_file), not a plain dict — this proves the refactor didn't
        change its existing (changeset-based) call contract."""
        class _SimpleChangeset:
            def __init__(self, files):
                self._files = files

            @property
            def changed_paths(self):
                return list(self._files)

            def read_file(self, rel):
                b = self._files.get(rel)
                return (b, 0o644) if b is not None else None

        text = build_diff_text(_SimpleChangeset({"a.py": b"A = 1\n"}))
        self.assertIn("=== FILE: a.py (proposed content) ===", text)
        self.assertIn("A = 1", text)


if __name__ == "__main__":
    unittest.main()
