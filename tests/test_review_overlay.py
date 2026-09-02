"""The reviewer reads the tree it judges.

It used to sit in the pre-change tree with the proposal as a diff and a side
directory, and spent a median 18 Bash calls per review navigating the wrong
one — once reporting a whole feature "missing" that the diff in front of it
added. Now the proposal is written into the reviewer's working copy before
its CLI starts, and custody checks the BYTES of those files instead of
exempting their paths.

Runnable with: python -m unittest tests.test_review_overlay
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.supervisor import _PreparingExecution, overlay_tree  # noqa: E402


class OverlayTree(unittest.TestCase):
    def test_files_land_at_their_relative_paths_with_the_exec_bit(self):
        src = Path(tempfile.mkdtemp(prefix="shepherd-ov-src-"))
        dst = Path(tempfile.mkdtemp(prefix="shepherd-ov-dst-"))
        (src / "lib" / "a").mkdir(parents=True)
        (src / "lib" / "a" / "x.ex").write_text("new\n")
        (src / "bin").mkdir()
        (src / "bin" / "run.sh").write_text("#!/bin/sh\n")
        (src / "bin" / "run.sh").chmod(0o755)
        (dst / "lib" / "a").mkdir(parents=True)
        (dst / "lib" / "a" / "x.ex").write_text("old\n")
        (dst / "lib" / "a" / "keep.ex").write_text("keep\n")
        self.assertEqual(overlay_tree(src, dst), 2)
        self.assertEqual((dst / "lib" / "a" / "x.ex").read_text(), "new\n")
        self.assertEqual((dst / "lib" / "a" / "keep.ex").read_text(), "keep\n")
        self.assertTrue((dst / "bin" / "run.sh").stat().st_mode & 0o111)

    def test_a_missing_source_is_nothing_not_an_error(self):
        dst = Path(tempfile.mkdtemp(prefix="shepherd-ov-dst-"))
        self.assertEqual(overlay_tree(Path("/nonexistent/proposed"), dst), 0)


class ThePreparingProxy(unittest.TestCase):
    def test_overlay_happens_before_the_launch_and_is_reported(self):
        src = Path(tempfile.mkdtemp(prefix="shepherd-ov-src-"))
        (src / "f.py").write_text("proposed\n")
        work = Path(tempfile.mkdtemp(prefix="shepherd-ov-work-"))
        seen = {}

        class _Inner:
            working_path = work

            def launch_confined(self, command, confinement):
                seen["at_launch"] = (work / "f.py").read_text()
                return "launched"

        events: list = []
        hook = SimpleNamespace(emit=lambda kind, payload: events.append((kind, payload)))
        proxy = _PreparingExecution(_Inner(), overlay_from=src, hook=hook)
        self.assertEqual(proxy.launch_confined(["x"], None), "launched")
        self.assertEqual(seen["at_launch"], "proposed\n")
        self.assertEqual(events, [("worker.overlay", {"files": 1, "role": "reviewer"})])

    def test_no_overlay_source_means_a_plain_launch(self):
        class _Inner:
            working_path = Path("/w")

            def launch_confined(self, command, confinement):
                return "launched"

        self.assertEqual(_PreparingExecution(_Inner(), overlay_from=None).launch_confined([], None), "launched")


class Custody(unittest.TestCase):
    """run_review's guard, driven by a fake workspace whose review changeset
    carries the proposal's paths back (as the overlay makes it do)."""

    def _verdict(self, proposal: dict[str, bytes], came_back: dict[str, bytes], drift=None):
        from shepherd_dev import supervisor as sup

        class _CS:
            def __init__(self, files):
                self._files = files

            @property
            def changed_paths(self):
                return list(self._files)

            def read_file(self, rel):
                b = self._files.get(rel)
                return (b, 0o100644) if b is not None else None

        class _Output:
            def changeset(self):
                return _CS(came_back)

            def discard(self):
                pass

        class _Run:
            def output(self):
                return _Output()

        class _Tasks:
            def register(self, task):
                pass

        class _Workspace:
            tasks = _Tasks()

            def git_repo(self):
                return None

            def run(self, task, **kw):
                return _Run()

        return sup.run_review(
            _Workspace(), object(), feature="f", changeset=_CS(proposal), workspace_drift=drift,
        )

    def test_the_proposals_own_files_coming_back_unchanged_are_not_tampering(self):
        v = self._verdict(
            {"lib/a.ex": b"proposed\n"},
            {"lib/a.ex": b"proposed\n", "REVIEW.json": b'{"approved": true, "summary": "ok", "issues": []}'},
        )
        self.assertIsNone(v.error)
        self.assertTrue(v.approved)

    def test_a_proposal_file_the_reviewer_edited_invalidates_the_verdict(self):
        v = self._verdict(
            {"lib/a.ex": b"proposed\n"},
            {"lib/a.ex": b"tampered\n", "REVIEW.json": b'{"approved": true, "summary": "ok", "issues": []}'},
        )
        self.assertIsNotNone(v.error)
        self.assertIn("lib/a.ex", v.error)

    def test_workspace_drift_outside_the_proposal_is_still_forgiven(self):
        v = self._verdict(
            {"lib/a.ex": b"proposed\n"},
            {"lib/a.ex": b"proposed\n", "lib/pending.ex": b"old proposal\n",
             "REVIEW.json": b'{"approved": true, "summary": "ok", "issues": []}'},
            drift={"lib/pending.ex"},
        )
        self.assertIsNone(v.error)

    def test_a_file_outside_both_is_tampering(self):
        v = self._verdict(
            {"lib/a.ex": b"proposed\n"},
            {"lib/a.ex": b"proposed\n", "lib/other.ex": b"x\n",
             "REVIEW.json": b'{"approved": true, "summary": "ok", "issues": []}'},
        )
        self.assertIsNotNone(v.error)
        self.assertIn("lib/other.ex", v.error)


class ThePromptSaysSo(unittest.TestCase):
    def test_the_review_prompt_describes_the_applied_tree(self):
        from shepherd_dev.prompts import get_prompt
        from shepherd_dev.promptrender import CLOSING

        text = get_prompt("review")
        self.assertIn("WITH the proposal applied", text)
        self.assertNotIn("CURRENT (pre-change) code", text)
        self.assertIn("WITH the proposal applied", CLOSING["review"])


try:
    from shepherd_dialect.workspace_control import runtime_provider as _rp

    _HAS_SUBSTRATE = True
except Exception:  # pragma: no cover - substrate absent
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class ThroughTheTransport(unittest.TestCase):
    def setUp(self):
        self._previous = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS

    def tearDown(self):
        _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = self._previous

    def test_only_a_review_launch_carries_an_overlay(self):
        from shepherd_dev.supervisor import set_worker_budget

        set_worker_budget(300)
        transport = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude
        review = transport(SimpleNamespace(
            provider_id="claude", prompt="p", model_name=None,
            task_lock=SimpleNamespace(task_id="shepherd_dev.tasks.review"),
            kwargs={"proposed_root": "/tmp/proposed-x", "diff": "", "feature": "f"},
        ))
        self.assertEqual(review._overlay_from, "/tmp/proposed-x")
        worker = transport(SimpleNamespace(
            provider_id="claude", prompt="p", model_name=None,
            task_lock=SimpleNamespace(task_id="shepherd_dev.tasks.implement"),
            kwargs={"feature": "f", "proposed_root": "/should/be/ignored"},
        ))
        self.assertIsNone(worker._overlay_from)


if __name__ == "__main__":
    unittest.main()
