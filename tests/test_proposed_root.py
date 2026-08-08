"""The reviewer must read the applied result, not rebuild it from the diff.

Seed: run-64aad3fcac0f. The reviewer rejected a proposal, stating that a
diff hunk reused a context `end` and left marcar_critico/3 unclosed, and
described its method precisely — "I reconstructed the patched file exactly as
the diff specifies (byte-for-byte, verified against the original file) and ran
it through Code.string_to_quoted". The gate had compiled the same proposal and
run 952 tests green in that same run, and the settled file parses.

The reviewer had rebuilt the file by hand, made the classic patch mistake of
treating a context line as removed, and then parsed its OWN reconstruction —
reporting the result as the worker's defect.

It had no alternative: `diff` is a unified diff (60cc363 made it one, to stop
whole-file content from eating the window), and prompts.py says plainly that
the proposal is NOT applied to the tree it reads. The applied result existed
nowhere it could reach. This gives it one.

Runnable with: python -m unittest tests.test_proposed_root
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

VERDICT = json.dumps(
    {"approved": True, "summary": "ok", "issues": [], "resolved": []}
).encode()


class _Output:
    def __init__(self, entries):
        self._entries = entries

    def changeset(self):
        return self._entries

    def discard(self):
        pass


class _Run:
    run_ref = "run-1"

    def __init__(self, entries):
        self._out = _Output(entries)

    def output(self):
        return self._out


class _Tasks:
    def register(self, task):
        pass


class _Workspace:
    """Captures the args the reviewer was handed."""

    tasks = _Tasks()

    def __init__(self, entries=None):
        self._entries = entries if entries is not None else {"REVIEW.json": VERDICT}
        self.args = None

    def git_repo(self):
        return None

    def run(self, task, **kw):
        self.args = dict(kw.get("args", {}))
        # Read while the run is in flight — the directory must be gone after.
        root = self.args.get("proposed_root")
        self.seen = {}
        if root:
            base = Path(root)
            if base.is_dir():
                for p in sorted(base.rglob("*")):
                    if p.is_file():
                        self.seen[p.relative_to(base).as_posix()] = p.read_bytes()
        return _Run(self._entries)


def _review(workspace, **kw):
    from shepherd_dev import supervisor as S

    kw.setdefault("feature", "add X")
    return S.run_review(workspace, object(), **kw)


class ProposalIsMaterialisedTests(unittest.TestCase):
    def setUp(self):
        from shepherd_dev import supervisor as S

        self._orig = S.read_changeset_entries
        S.read_changeset_entries = lambda cs: dict(cs)
        self.addCleanup(lambda: setattr(S, "read_changeset_entries", self._orig))

    PROPOSAL = {
        "lib/sac/chamados.ex": b"defmodule Sac.Chamados do\n  def escalar, do: :ok\nend\n",
        "test/sac/chamados_test.exs": b"defmodule T do\nend\n",
    }

    def test_every_proposed_file_is_on_disk_with_its_proposed_content(self):
        ws = _Workspace()
        _review(ws, changeset=self.PROPOSAL, diff_text="(a diff)")
        self.assertEqual(ws.seen, self.PROPOSAL)

    def test_nested_directories_are_created(self):
        ws = _Workspace()
        _review(ws, changeset={"a/b/c/deep.ex": b"x\n"}, diff_text="d")
        self.assertEqual(ws.seen, {"a/b/c/deep.ex": b"x\n"})

    def test_the_reviewer_is_told_where_it_is(self):
        ws = _Workspace()
        _review(ws, changeset=self.PROPOSAL, diff_text="d")
        root = ws.args["proposed_root"]
        self.assertTrue(root, "the arg must carry a path")
        self.assertTrue(Path(root).is_absolute(), root)

    def test_it_is_cleaned_up_when_the_review_ends(self):
        ws = _Workspace()
        _review(ws, changeset=self.PROPOSAL, diff_text="d")
        self.assertFalse(Path(ws.args["proposed_root"]).exists())

    def test_it_is_cleaned_up_even_when_the_run_raises(self):
        from shepherd_dev import supervisor as S

        seen = {}

        class _Boom(_Workspace):
            def run(self, task, **kw):
                seen["root"] = kw["args"]["proposed_root"]
                raise RuntimeError("provider died")

        verdict = S.run_review(
            _Boom(), object(), feature="f", changeset=self.PROPOSAL, diff_text="d"
        )
        self.assertIsNotNone(verdict.error)
        self.assertFalse(Path(seen["root"]).exists())

    def test_the_diff_is_still_sent(self):
        """The result is for checking; the diff is still how the reviewer sees
        WHAT changed without reading every file."""
        ws = _Workspace()
        _review(ws, changeset=self.PROPOSAL, diff_text="=== CHANGED FILES (2) ===")
        self.assertIn("CHANGED FILES", ws.args["diff"])

    def test_a_path_escaping_the_root_is_refused(self):
        """A changeset path is worker-controlled. `../` in one would write
        outside the temporary directory."""
        ws = _Workspace()
        _review(ws, changeset={"../escaped.ex": b"x\n"}, diff_text="d")
        self.assertEqual(ws.seen, {}, "nothing outside the root may be written")

    def test_an_empty_changeset_still_gets_a_root(self):
        ws = _Workspace()
        _review(ws, changeset={}, diff_text="d")
        self.assertIsNotNone(ws.args.get("proposed_root"))


class PromptForbidsHandRebuildingTests(unittest.TestCase):
    def test_the_prompt_names_the_argument(self):
        from shepherd_dev.prompts import get_prompt

        self.assertIn("proposed_root", get_prompt("review"))

    def test_the_prompt_forbids_reconstructing_the_result(self):
        from shepherd_dev.prompts import get_prompt

        prompt = " ".join(get_prompt("review").split()).lower()
        self.assertIn("never rebuild", prompt)
        self.assertIn("your reconstruction is not", prompt)

    def test_the_review_task_accepts_it(self):
        import inspect

        from shepherd_dev import tasks

        fn = getattr(tasks.review, "__wrapped__", None) or tasks.review
        params = inspect.signature(fn).parameters
        self.assertIn("proposed_root", params)
        self.assertEqual(params["proposed_root"].default, "")


if __name__ == "__main__":
    unittest.main()
