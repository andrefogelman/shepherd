"""The reviewer's custody guard must accuse only the reviewer.

Seed case (run 20260806-215606-aca7a1, repo sac): the guard reported
"reviewer touched files beyond REVIEW.json: ['REVIEW.json',
'lib/sac/chamados.ex', 'lib/sac_web/router.ex']" and threw the verdict
away. The reviewer had written neither file. Reproduced with a run that
wrote ONE file into that workspace: its changeset came back carrying five
paths of the workspace's own pending proposal before the run touched
anything, of which the two readable ones survived the entry filter.

Runnable with: python -m unittest tests.test_review_custody
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

VERDICT = json.dumps(
    {"approved": True, "summary": "looks right", "issues": [], "resolved": []}
).encode()


class _Output:
    def __init__(self, entries):
        self._entries = entries
        self.discarded = False

    def changeset(self):
        return self._entries

    def discard(self):
        self.discarded = True


class _Run:
    run_ref = "run-review-1"

    def __init__(self, entries):
        self._out = _Output(entries)

    def output(self):
        return self._out


class _Tasks:
    def register(self, task):
        pass


class _Workspace:
    """Fakes the substrate at the same boundary the other review tests do:
    workspace.run returns a run whose changeset is a dict of entries."""

    tasks = _Tasks()

    def __init__(self, entries, drift=()):
        self._entries = entries
        self._drift = list(drift)
        self.runs = 0

    def git_repo(self):
        return None

    def run(self, task, **kw):
        self.runs += 1
        return _Run(self._entries)


def _run_review(workspace, **kw):
    from shepherd_dev import supervisor as S

    kw.setdefault("feature", "add X")
    kw.setdefault("diff_text", "+x")
    return S.run_review(workspace, object(), **kw)


class DriftIsNotATouchTests(unittest.TestCase):
    def setUp(self):
        from shepherd_dev import supervisor as S

        self._orig = S.read_changeset_entries
        S.read_changeset_entries = lambda cs: dict(cs)
        self.addCleanup(lambda: setattr(S, "read_changeset_entries", self._orig))

    def test_the_workspaces_own_pending_paths_do_not_invalidate_the_verdict(self):
        """The seed case, at unit scale."""
        ws = _Workspace({
            "REVIEW.json": VERDICT,
            "lib/sac/chamados.ex": b"stale basis content\n",
            "lib/sac_web/router.ex": b"stale basis content\n",
        })
        verdict = _run_review(
            ws, workspace_drift={"lib/sac/chamados.ex", "lib/sac_web/router.ex"},
        )
        self.assertIsNone(verdict.error, "drift is not the reviewer's doing")
        self.assertTrue(verdict.approved)
        self.assertEqual(verdict.summary, "looks right")

    def test_a_file_the_reviewer_really_wrote_still_invalidates(self):
        """The guard must keep working — drift subtraction is not amnesty."""
        ws = _Workspace({
            "REVIEW.json": VERDICT,
            "lib/sac/chamados.ex": b"stale\n",
            "lib/sac/rogue.ex": b"the reviewer edited this\n",
        })
        verdict = _run_review(ws, workspace_drift={"lib/sac/chamados.ex"})
        self.assertIsNotNone(verdict.error)
        self.assertIn("rogue.ex", verdict.error)
        self.assertNotIn(
            "chamados.ex", verdict.error, "drift must not appear in the accusation"
        )
        self.assertFalse(verdict.approved)

    def test_no_drift_given_behaves_exactly_as_before(self):
        ws = _Workspace({"REVIEW.json": VERDICT, "lib/x.ex": b"y\n"})
        self.assertIsNotNone(_run_review(ws).error)

    def test_a_missing_review_json_is_still_an_error_even_under_drift(self):
        ws = _Workspace({"lib/sac/chamados.ex": b"stale\n"})
        verdict = _run_review(ws, workspace_drift={"lib/sac/chamados.ex"})
        self.assertIsNotNone(verdict.error)
        self.assertIn("REVIEW.json", verdict.error)


class ScratchIsNotATouchTests(unittest.TestCase):
    """The reviewer has to reconstruct files to check they parse. Today the
    only place it can write is inside the tree it is reviewing, so its
    scratch is indistinguishable from tampering."""

    def setUp(self):
        from shepherd_dev import supervisor as S

        self._orig = S.read_changeset_entries
        S.read_changeset_entries = lambda cs: dict(cs)
        self.addCleanup(lambda: setattr(S, "read_changeset_entries", self._orig))

    def test_the_scratch_directory_is_named_and_exempt(self):
        from shepherd_dev.supervisor import REVIEW_SCRATCH_DIR

        ws = _Workspace({
            "REVIEW.json": VERDICT,
            f"{REVIEW_SCRATCH_DIR}/chamados.ex.orig": b"reconstructed\n",
            f"{REVIEW_SCRATCH_DIR}/nested/deep.diff": b"a diff\n",
        })
        verdict = _run_review(ws)
        self.assertIsNone(verdict.error)
        self.assertTrue(verdict.approved)

    def test_a_lookalike_path_outside_the_scratch_dir_is_not_exempt(self):
        """`.review-scratch-evil/x` and `lib/.review-scratch` must not pass."""
        from shepherd_dev.supervisor import REVIEW_SCRATCH_DIR

        for rel in (f"{REVIEW_SCRATCH_DIR}-evil/x.ex", f"lib/{REVIEW_SCRATCH_DIR}/x.ex"):
            with self.subTest(path=rel):
                ws = _Workspace({"REVIEW.json": VERDICT, rel: b"x\n"})
                self.assertIsNotNone(_run_review(ws).error)

    def test_the_prompt_tells_the_reviewer_where_to_scratch(self):
        from shepherd_dev.prompts import get_prompt
        from shepherd_dev.supervisor import REVIEW_SCRATCH_DIR

        prompt = " ".join(get_prompt("review").split())
        self.assertIn(REVIEW_SCRATCH_DIR, prompt)


class InvalidationKeepsTheAnalysisTests(unittest.TestCase):
    """Invalidating the verdict is defensible; throwing away the reviewer's
    written analysis is not. In the seed case a 3656-byte REVIEW.json was
    discarded with the clone moments later."""

    def setUp(self):
        from shepherd_dev import supervisor as S

        self._orig = S.read_changeset_entries
        S.read_changeset_entries = lambda cs: dict(cs)
        self.addCleanup(lambda: setattr(S, "read_changeset_entries", self._orig))

    def test_an_invalidated_review_json_is_saved_before_the_output_is_discarded(self):
        from tmpdirs import mkdtemp

        keep = Path(mkdtemp(prefix="shepherd-keep-"))
        ws = _Workspace({"REVIEW.json": VERDICT, "lib/rogue.ex": b"x\n"})
        verdict = _run_review(ws, salvage_dir=keep)
        self.assertIsNotNone(verdict.error)
        saved = keep / "REVIEW.invalidated.json"
        self.assertTrue(saved.is_file(), "the analysis must outlive the clone")
        self.assertEqual(saved.read_bytes(), VERDICT)
        self.assertIn(str(saved), verdict.error, "the error must say where it went")

    def test_salvage_failure_never_costs_the_verdict(self):
        """A read-only salvage dir must not turn one failure into two."""
        ws = _Workspace({"REVIEW.json": VERDICT, "lib/rogue.ex": b"x\n"})
        verdict = _run_review(ws, salvage_dir=Path("/nonexistent/\0bad"))
        self.assertIsNotNone(verdict.error)
        self.assertIn("rogue.ex", verdict.error)

    def test_nothing_is_written_when_the_verdict_is_valid(self):
        from tmpdirs import mkdtemp

        keep = Path(mkdtemp(prefix="shepherd-keep-"))
        ws = _Workspace({"REVIEW.json": VERDICT})
        self.assertIsNone(_run_review(ws, salvage_dir=keep).error)
        self.assertEqual(list(keep.iterdir()), [])


class UnavailableIsNotRejectedTests(unittest.TestCase):
    """`approved: false` and "no verdict" are different facts. The event log
    recorded the first for the second, so a reader — human or tooling — saw
    a rejection where there was only an absence."""

    def test_the_recorded_verdict_carries_the_error(self):
        from shepherd_dev import supervisor as S

        seen = []

        class _Log:
            def emit(self, kind, payload=None, attempt=None):
                seen.append((kind, payload))

        verdict = S.ReviewVerdict(
            approved=False, summary="", issues=[], error="review unavailable: boom"
        )
        S.emit_review_verdict(_Log(), verdict, attempt=1)
        kinds = dict((k, p) for k, p in seen)
        self.assertIn("review.verdict", kinds)
        payload = kinds["review.verdict"]
        self.assertEqual(payload.get("error"), "review unavailable: boom")
        self.assertIsNone(
            payload.get("approved"),
            "no verdict is not a rejection — approved must be null, never false",
        )

    def test_a_real_rejection_still_records_false(self):
        from shepherd_dev import supervisor as S

        seen = []

        class _Log:
            def emit(self, kind, payload=None, attempt=None):
                seen.append((kind, payload))

        S.emit_review_verdict(
            _Log(),
            S.ReviewVerdict(approved=False, summary="bad", issues=["x"]),
            attempt=1,
        )
        payload = dict(seen)["review.verdict"]
        self.assertIs(payload["approved"], False)
        self.assertIsNone(payload.get("error"))


if __name__ == "__main__":
    unittest.main()
