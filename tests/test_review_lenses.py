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


try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class PanelDispatchesLensesTests(unittest.TestCase):
    """Only the reviewer's own AI call is faked; the clones are real, the
    same boundary the existing panel tests fake at."""

    def setUp(self):
        import subprocess

        from tmpdirs import mkdtemp

        from shepherd_dev import supervisor as S

        self.repo = Path(mkdtemp(prefix="shepherd-lens-"))
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("V = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli", "init", "--repo", str(self.repo)],
            input="", capture_output=True, text=True, check=True,
        )
        self._orig = S.run_review
        self.addCleanup(lambda: setattr(S, "run_review", self._orig))

    def _fake_reviews(self, verdict_for=None):
        """Record the lens each reviewer was given."""
        from shepherd_dev import supervisor as S

        seen: list[str] = []

        def _fake(workspace, review_task, **kw):
            lens = kw.get("lens", "")
            seen.append(lens)
            if verdict_for is not None:
                return verdict_for(lens)
            return S.ReviewVerdict(approved=True, summary=f"{lens or 'generic'} ok", issues=[])

        S.run_review = _fake
        return seen

    def test_each_named_lens_reaches_exactly_one_reviewer(self):
        from shepherd_dev import supervisor as S

        seen = self._fake_reviews()
        verdict = S.run_review_panel(
            self.repo, object(), ["correctness", "security"], feature="add X",
        )
        self.assertEqual(sorted(seen), ["correctness", "security"])
        self.assertTrue(verdict.approved)

    def test_a_numeric_panel_is_unlabelled_reviewers(self):
        """The pre-existing behavior, now expressed as empty lenses."""
        from shepherd_dev import supervisor as S

        seen = self._fake_reviews()
        S.run_review_panel(self.repo, object(), ["", "", ""], feature="add X")
        self.assertEqual(seen, ["", "", ""])

    def test_one_dissenting_lens_blocks_and_its_issue_survives(self):
        from shepherd_dev import supervisor as S

        def _verdict(lens):
            if lens == "security":
                return S.ReviewVerdict(
                    approved=False, summary="unsafe", issues=["secret reaches the log"]
                )
            return S.ReviewVerdict(approved=True, summary="fine", issues=[])

        self._fake_reviews(_verdict)
        verdict = S.run_review_panel(
            self.repo, object(), ["correctness", "security", "tests"], feature="add X",
        )
        self.assertFalse(verdict.approved, "unanimity: one lens objecting blocks")
        self.assertIn("secret reaches the log", verdict.issues)

    def test_an_empty_lens_list_is_an_error_not_a_silent_approval(self):
        from shepherd_dev import supervisor as S

        self._fake_reviews()
        verdict = S.run_review_panel(self.repo, object(), [], feature="add X")
        self.assertFalse(verdict.approved)
        self.assertIsNotNone(verdict.error)


class RunReviewPassesLensTests(unittest.TestCase):
    def test_the_lens_reaches_the_task_arguments(self):
        """run_review must forward `lens` into the task args, or the whole
        feature is inert: the reviewer would never see its assignment."""
        from unittest.mock import MagicMock

        from shepherd_dev import supervisor as S

        captured = {}

        class _Tasks:
            def register(self, task):
                pass

        class _WS:
            tasks = _Tasks()

            def git_repo(self):
                return None

            def run(self, task, **kw):
                captured.update(kw.get("args", {}))
                raise RuntimeError("stop here — the args are what we came for")

        verdict = S.run_review(
            _WS(), MagicMock(), feature="add X", diff_text="+x", lens="security",
        )
        self.assertEqual(captured.get("lens"), "security")
        self.assertIsNotNone(verdict.error)  # the raise became an error verdict


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class DevelopLensWiringTests(unittest.TestCase):
    """develop() driven by fakes — the same harness shape as
    test_review_rounds.py's loop tests. These check ROUTING only."""

    def _run(self, *, review_panel=1, review_lenses=None):
        from shepherd_dev import supervisor as sup

        calls = {"single": 0, "panel": 0, "lenses": None}

        class _Output:
            def changeset(self):
                return {"file.py": b"v1\n"}

            def discard(self):
                pass

        class _Run:
            run_ref = "run-1"

            def output(self):
                return _Output()

        class _Tasks:
            def register(self, task):
                pass

        class _Workspace:
            tasks = _Tasks()

            def run(self, task, **kw):
                return _Run()

        def _gate(repo_root, entries, test_cmd, timeout, **kw):
            return sup.GateResult(True, 0, "ok")

        def _single(workspace, review_task, **kw):
            calls["single"] += 1
            return sup.ReviewVerdict(approved=True, summary="s", issues=[])

        def _panel(repo_root, review_task, lenses, **kw):
            calls["panel"] += 1
            calls["lenses"] = list(lenses)
            return sup.ReviewVerdict(approved=True, summary="p", issues=[])

        orig = (
            sup.read_changeset_entries, sup._run_gate, sup.run_review,
            sup.run_review_panel, sup._start_gate_warmup,
        )
        sup.read_changeset_entries = dict
        sup._run_gate = _gate
        sup.run_review = _single
        sup.run_review_panel = _panel
        sup._start_gate_warmup = lambda *a, **k: None
        try:
            sup.develop(
                _Workspace(), object(), repo=object(), repo_root=Path("/r"),
                feature="add X", test_cmd="pytest -q", review_task=object(),
                max_attempts=1, review_panel=review_panel, review_lenses=review_lenses,
            )
        finally:
            (
                sup.read_changeset_entries, sup._run_gate, sup.run_review,
                sup.run_review_panel, sup._start_gate_warmup,
            ) = orig
        return calls

    def test_no_lenses_and_no_panel_is_still_one_plain_reviewer(self):
        calls = self._run()
        self.assertEqual((calls["single"], calls["panel"]), (1, 0))

    def test_named_lenses_route_to_the_panel_verbatim(self):
        calls = self._run(review_lenses=["security", "tests"])
        self.assertEqual((calls["single"], calls["panel"]), (0, 1))
        self.assertEqual(calls["lenses"], ["security", "tests"])

    def test_a_numeric_panel_becomes_that_many_unlabelled_reviewers(self):
        calls = self._run(review_panel=3)
        self.assertEqual(calls["lenses"], ["", "", ""])

    def test_lenses_win_over_a_numeric_panel_rather_than_multiplying(self):
        """Both set is refused at the CLI, but develop() must not silently
        run 3x2 reviewers if some other caller passes both."""
        calls = self._run(review_panel=3, review_lenses=["security"])
        self.assertEqual(calls["lenses"], ["security"])

    def test_the_default_is_none_not_a_mutable_list(self):
        import inspect

        from shepherd_dev import supervisor as sup

        self.assertIsNone(inspect.signature(sup.develop).parameters["review_lenses"].default)


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class ReviewLensCliTests(unittest.TestCase):
    def test_the_flag_defaults_to_no_lenses(self):
        """Opt-in per run: a run that does not name a lens must behave
        exactly as it does today."""
        from shepherd_dev.cli import build_parser

        self.assertEqual(build_parser().parse_args(["run", "add X"]).review_lens, [])

    def test_the_flag_repeats(self):
        from shepherd_dev.cli import build_parser

        args = build_parser().parse_args(
            ["run", "add X", "--review-lens", "correctness", "--review-lens", "security"]
        )
        self.assertEqual(args.review_lens, ["correctness", "security"])

    def test_an_unknown_lens_is_refused_by_name(self):
        from shepherd_dev.cli import _validate_review_lenses

        reason = _validate_review_lenses(
            ["banana"], no_review=False, provider="claude", best_of=1, explicit_panel=None
        )
        self.assertIsNotNone(reason)
        self.assertIn("banana", reason)
        self.assertIn("correctness", reason, "the error must list the valid names")

    def test_the_valid_names_are_accepted(self):
        from shepherd_dev.cli import _validate_review_lenses
        from shepherd_dev.prompts import LENS_NAMES

        self.assertIsNone(
            _validate_review_lenses(
                list(LENS_NAMES), no_review=False, provider="claude", best_of=1, explicit_panel=None
            )
        )

    def test_a_repeated_lens_is_refused_rather_than_run_twice(self):
        from shepherd_dev.cli import _validate_review_lenses

        reason = _validate_review_lenses(
            ["security", "security"], no_review=False, provider="claude", best_of=1, explicit_panel=None
        )
        self.assertIsNotNone(reason)

    def test_lenses_do_not_combine_with_an_explicit_panel(self):
        """Both are panels; accepting both would silently honour neither —
        the same reasoning as --review-panel vs --best-of."""
        from shepherd_dev.cli import _validate_review_lenses

        reason = _validate_review_lenses(
            ["security"], no_review=False, provider="claude", best_of=1, explicit_panel=3
        )
        self.assertIsNotNone(reason)
        self.assertIn("--review-panel", reason)

    def test_a_saved_panel_value_does_not_block_lenses(self):
        """Only an EXPLICIT --review-panel conflicts. A repo's saved value
        must not make --review-lens unusable in that repo."""
        from shepherd_dev.cli import _validate_review_lenses

        self.assertIsNone(
            _validate_review_lenses(
                ["security"], no_review=False, provider="claude", best_of=1, explicit_panel=None
            )
        )

    def test_lenses_need_a_reviewer_and_a_reviewing_provider(self):
        from shepherd_dev.cli import _validate_review_lenses

        self.assertIsNotNone(
            _validate_review_lenses(
                ["security"], no_review=True, provider="claude", best_of=1, explicit_panel=None
            )
        )
        self.assertIsNotNone(
            _validate_review_lenses(
                ["security"], no_review=False, provider="static", best_of=1, explicit_panel=None
            )
        )

    def test_lenses_do_not_combine_with_best_of(self):
        from shepherd_dev.cli import _validate_review_lenses

        self.assertIsNotNone(
            _validate_review_lenses(
                ["security"], no_review=False, provider="claude", best_of=2, explicit_panel=None
            )
        )

    def test_no_lenses_is_always_usable(self):
        """The default path must never be refused by this validator."""
        from shepherd_dev.cli import _validate_review_lenses

        self.assertIsNone(
            _validate_review_lenses(
                [], no_review=True, provider="static", best_of=3, explicit_panel=None
            )
        )


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class MenuKnowsTheFlagTests(unittest.TestCase):
    def test_the_flag_is_classified_in_the_menu_table(self):
        from shepherd_dev.menu import OPTIONS

        opt = next(o for o in OPTIONS["run"] if o.flag == "--review-lens")
        self.assertEqual(opt.kind, "list")
        self.assertEqual(opt.dest, "review_lens")

    def test_the_menu_builds_a_repeated_flag_that_parses(self):
        from shepherd_dev.cli import build_parser
        from shepherd_dev.menu import build_argv

        argv = build_argv("run", {"feature": "add X", "review_lens": ["correctness", "tests"]})
        self.assertEqual(build_parser().parse_args(argv).review_lens, ["correctness", "tests"])


if __name__ == "__main__":
    unittest.main()
