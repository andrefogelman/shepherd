# Lens-Differentiated Review Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `shepherd-dev run --review-lens correctness --review-lens security` puts one reviewer on each named dimension instead of K reviewers sharing one prompt, so the panel's disagreement comes from genuinely different questions rather than from sampling noise.

**Architecture:** The panel already exists (`run_review_panel`) and already aggregates under unanimity. This adds the missing axis: a reviewer can be told which single dimension it owns. `run_review_panel`'s `size: int` becomes `lenses: list[str]` — the numeric panel becomes `[""] * K` (K unlabelled reviewers, today's exact behavior) and a lens panel is `["correctness", "security"]`. One function, one aggregation path, no duplication.

**Tech Stack:** Python 3.11+ stdlib only (`argparse`, `unittest`). No new dependency — this machine forbids installing anything.

## Why this exists

The current panel runs K reviewers on **the same prompt**. Diversity comes only from sampling non-determinism, so K samples of one bias correlate: the blind spot the prompt has, all K share. A panel is supposed to buy independent lenses; today it buys repetition.

## Global Constraints

- **Opt-in per run, off by default.** `--review-lens` defaults to an empty list. A run that does not name a lens behaves exactly as it does today. No repo config, no saved default, nothing inherited from `init` — the user signals it on each run or it does not happen.
- No new third-party dependency; stdlib only. Installing anything is forbidden on this machine.
- The existing numeric `--review-panel K` path must keep working unchanged, and its tests must pass untouched.
- Unanimity stays the aggregation rule: every reviewer must approve. A single lens objecting blocks — that is the point of having lenses.
- **`--review-lens` must NOT declare argparse `choices=`.** The menu's drift test (`tests/test_menu.py:100-104`) asserts `kind == "list"` iff the action is `_AppendAction` **and** `kind == "choice"` iff `action.choices is not None`. An append action carrying choices satisfies both, so no `kind` value can pass. Validate lens names in `_validate_review_lenses` instead — which also yields a better error than argparse's.
- Every new CLI flag must be classified in `src/shepherd_dev/menu.py`'s `OPTIONS` table in the SAME task that adds it, or `tests/test_menu.py::test_every_flag_is_classified` fails.
- Lens texts live in `prompts.py` but are NOT added to `PROMPT_KEYS` or `optimize.py`'s `EDITABLE_KEYS`: those are the tunable core prompts, and a lens catalogue is a taxonomy, not a prompt under optimization.

## File Structure

| File | Responsibility |
|---|---|
| `src/shepherd_dev/prompts.py` (modify) | `REVIEW_LENSES` catalogue; the `review` prompt gains a paragraph explaining the `lens` arg. |
| `src/shepherd_dev/tasks.py` (modify) | The `review` task signature gains `lens: str = ""`. |
| `src/shepherd_dev/supervisor.py` (modify) | `run_review` passes `lens` through; `run_review_panel` takes `lenses: list[str]`; `develop` gains `review_lenses`. |
| `src/shepherd_dev/cli.py` (modify) | `--review-lens` flag, `_validate_review_lenses`, wiring into `develop`. |
| `src/shepherd_dev/menu.py` (modify) | Classify the new flag in `OPTIONS["run"]`. |
| `tests/test_review_lenses.py` (create) | The catalogue, argv/validation, and the panel's lens dispatch. |
| `docs/MANUAL.md`, `docs/MANUAL.en.md` (modify) | Document it. |

---

### Task 1: The lens catalogue and the reviewer's `lens` argument

**Files:**
- Modify: `src/shepherd_dev/prompts.py`
- Modify: `src/shepherd_dev/tasks.py`
- Test: `tests/test_review_lenses.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `REVIEW_LENSES: dict[str, str]` in `prompts.py`, mapping a lens name to its instruction text; `LENS_NAMES: tuple[str, ...]` (its keys, in catalogue order) — Tasks 2 and 4 both read these. The `review` task signature becomes `review(repo, feature, diff, context="", findings="", lens="")`.

The five lenses are not invented here: they are the dimensions the review prompt already lists in one breath ("correctness, hidden bugs, security issues, scope discipline, convention adherence, and missing edge cases"). Splitting that sentence into separate reviewers IS the feature.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review_lenses.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest tests.test_review_lenses -v`
Expected: FAIL with `ImportError: cannot import name 'LENS_NAMES'`

Note on `ReviewTaskSignatureTests`: `tasks.review` is wrapped by `sp.task(...)`. If `__wrapped__` is absent and `inspect.signature` cannot see the parameters, read the source function instead — the point of the test is that the signature grew a `lens` parameter defaulting to `""`, so assert that however the wrapper permits. Say in your report which form you used.

- [ ] **Step 3: Add the catalogue to `prompts.py`**

Insert above `DEFAULT_PROMPTS`:

```python
#: One reviewer per dimension, instead of K reviewers sharing one prompt.
#:
#: These are not a new taxonomy: they are the dimensions the generic review
#: prompt already lists in a single sentence ("correctness, hidden bugs,
#: security issues, scope discipline, convention adherence, and missing edge
#: cases"). Asking one reviewer to weigh all of them at once is what makes K
#: samples of that prompt correlate — they share its blind spot. Splitting
#: the sentence into separate reviewers is the whole feature.
#:
#: Each text ends by confining the reviewer to its lane. Without that, every
#: lens drifts back into a general audit and the panel is K correlated
#: samples again, only more expensive.
REVIEW_LENSES: dict[str, str] = {
    "correctness": (
        "You are the CORRECTNESS reviewer. Ask only whether this change does "
        "what it claims: wrong logic, off-by-one, an unhandled None, a branch "
        "that cannot be reached, state mutated where a copy was meant, an "
        "error swallowed, a race between two things that look sequential. "
        "Trace the actual values through the changed code rather than reading "
        "it for plausibility. Report ONLY correctness defects — another "
        "reviewer owns security, scope, conventions and tests."
    ),
    "security": (
        "You are the SECURITY reviewer. Ask only what an attacker or a "
        "hostile input could do with this change: injection through an "
        "unescaped value, a secret or token reaching a log or an error "
        "message, a path that escapes its root, a permission or ownership "
        "check that is missing or can be skipped, an unsafe default, a "
        "dependency or subprocess invoked with attacker-influenced "
        "arguments. Report ONLY security defects — another reviewer owns "
        "correctness, scope, conventions and tests."
    ),
    "scope": (
        "You are the SCOPE reviewer. Ask only whether this change is the "
        "change that was asked for: files touched that the feature does not "
        "need, refactoring smuggled in beside the fix, a behavior altered "
        "for callers who did not ask, a public signature or output format "
        "changed without cause, dead code or debugging residue left behind. "
        "Judge against the stated feature, not against your taste. Report "
        "ONLY scope defects — another reviewer owns correctness, security, "
        "conventions and tests."
    ),
    "conventions": (
        "You are the CONVENTIONS reviewer. Ask only whether this change "
        "reads like the code around it: naming, structure and error "
        "handling that match the surrounding module, an existing helper "
        "reimplemented instead of reused, a comment that states what the "
        "code plainly says rather than why it is that way, a comment that "
        "no longer matches the code beneath it. Compare against the actual "
        "neighbouring files, not a general style guide. Report ONLY "
        "convention defects — another reviewer owns correctness, security, "
        "scope and tests."
    ),
    "tests": (
        "You are the TEST reviewer. Ask only whether this change is "
        "genuinely covered: a new behavior with no test, a test that asserts "
        "nothing or would pass against the unfixed code, a mock standing in "
        "for the very thing under test, an edge case named in the code but "
        "absent from the tests, a test whose name promises more than it "
        "checks. A bug fix with no test that fails without it is the "
        "clearest case. Report ONLY test defects — another reviewer owns "
        "correctness, security, scope and conventions."
    ),
}

#: Catalogue order — what `--review-lens` accepts, and the order a panel runs.
LENS_NAMES: tuple[str, ...] = tuple(REVIEW_LENSES)
```

- [ ] **Step 4: Teach the review prompt about `lens`**

In `DEFAULT_PROMPTS["review"]`, immediately after the paragraph beginning "Assess: correctness, hidden bugs, security issues", insert:

```
    `lens`, when non-empty, narrows you to ONE of those dimensions and
    names it. Obey it literally: report only defects of that kind, and
    approve when you find none of that kind, even if something else about
    the change bothers you — a reviewer with a different lens is looking
    at the change at the same time and owns what you are leaving alone.
    When `lens` is empty you own all of the dimensions above, which is the
    ordinary single-reviewer case.
```

- [ ] **Step 5: Add the parameter to the review task**

In `src/shepherd_dev/tasks.py`, change the `review` signature:

```python
def review(repo: sp.GitRepo, feature: str, diff: str, context: str = "", findings: str = "", lens: str = "") -> None: ...
review.__doc__ = get_prompt("review")
review = sp.task(review)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest tests.test_review_lenses -v`
Expected: all 7 tests PASS

- [ ] **Step 7: Run the full suite**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest discover -s tests`
Expected: OK. The `review` task gained a defaulted parameter, so every existing caller is unaffected — a failure here means the wrapper does not tolerate the new parameter, which you must report rather than work around.

- [ ] **Step 8: Commit**

```bash
git add src/shepherd_dev/prompts.py src/shepherd_dev/tasks.py tests/test_review_lenses.py
git commit -m "feat: a lens catalogue, and a reviewer that can be given one"
```

---

### Task 2: `run_review_panel` dispatches lenses

**Files:**
- Modify: `src/shepherd_dev/supervisor.py`
- Test: `tests/test_review_lenses.py` (append)

**Interfaces:**
- Consumes: `REVIEW_LENSES`, `LENS_NAMES` (Task 1).
- Produces: `run_review(workspace, review_task, *, feature, changeset=None, diff_text=None, provider="claude", placement="jail", context_pack=None, findings="", lens="") -> ReviewVerdict` — one new keyword-only parameter, defaulted. And `run_review_panel(repo_root, review_task, lenses: list[str], *, feature, changeset=None, diff_text=None, provider="claude", placement="jail", context_pack=None, findings="") -> ReviewVerdict` — its third positional parameter changes from `size: int` to `lenses: list[str]`. Task 3 calls both.

The signature change is the point, not incidental: a numeric panel of K becomes `[""] * K`, so there is exactly one panel implementation and one aggregation path rather than two that can drift.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_lenses.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest tests.test_review_lenses -v`
Expected: FAIL — `run_review()` rejects the unexpected `lens` keyword, and `run_review_panel` still expects an int.

- [ ] **Step 3: Forward the lens through `run_review`**

In `src/shepherd_dev/supervisor.py`, add `lens: str = ""` as the last keyword-only parameter of `run_review`, and add it to the `args` dict passed to `workspace.run`:

```python
            args={
                "feature": feature,
                "diff": diff_text,
                "context": context_pack or "",
                "findings": findings,
                "lens": lens,
            },
```

- [ ] **Step 4: Make the panel take lenses**

Change `run_review_panel`'s third parameter from `size: int` to `lenses: list[str]`, and dispatch one reviewer per entry. Replace the clone-and-run body:

```python
    if diff_text is None:
        diff_text = build_diff_text(changeset) if changeset is not None else ""

    if not lenses:
        # Not a silent single review, and not an approval: a caller asking
        # for a panel of nothing has a bug, and inventing a verdict for it
        # would hide that behind an approval nobody reviewed.
        return ReviewVerdict(approved=False, summary="", error="review panel: no reviewers requested")

    try:
        clones = _clone_many(repo_root, len(lenses))
    except Exception as exc:
        return ReviewVerdict(approved=False, summary="", error=f"review panel could not create clones: {exc}")
    try:
        def _one(pair: tuple[Path, str]) -> ReviewVerdict:
            clone, lens = pair
            with sp.open(clone) as ws:
                return run_review(
                    ws,
                    review_task,
                    feature=feature,
                    diff_text=diff_text,
                    provider=provider,
                    placement=placement,
                    context_pack=context_pack,
                    findings=findings,
                    lens=lens,
                )

        with ThreadPoolExecutor(max_workers=len(lenses)) as pool:
            verdicts = list(pool.map(_one, zip(clones, lenses)))
    finally:
        for clone in clones:
            shutil.rmtree(clone.parent, ignore_errors=True)
    return _aggregate_review_verdicts(verdicts)
```

Update the docstring to say the third argument is one entry per reviewer, `""` meaning an unlabelled generic reviewer.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest tests.test_review_lenses -v`
Expected: all tests through Task 2 PASS

- [ ] **Step 6: Run the full suite — expect the numeric-panel callers to fail**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest discover -s tests`

`develop()` still passes an int, so its call and any test faking `run_review_panel` with an int will fail. That is expected at this point and Task 3 fixes it. **Report the failures you saw** rather than reaching into `develop()` here — that is the next task's reviewed change.

- [ ] **Step 7: Commit**

```bash
git add src/shepherd_dev/supervisor.py tests/test_review_lenses.py
git commit -m "feat: the review panel dispatches one lens per reviewer"
```

---

### Task 3: `develop()` chooses lenses over a count

**Files:**
- Modify: `src/shepherd_dev/supervisor.py` (`develop`'s signature, the speculative-review gate, the review dispatch)
- Test: `tests/test_review_lenses.py` (append)

**Interfaces:**
- Consumes: `run_review_panel(repo_root, review_task, lenses, ...)` (Task 2).
- Produces: `develop(..., review_panel: int = 1, review_lenses: list[str] | None = None, ...)`. Task 4's CLI passes `review_lenses`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_lenses.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest tests.test_review_lenses.DevelopLensWiringTests -v`
Expected: FAIL — `develop()` has no `review_lenses` parameter.

- [ ] **Step 3: Wire it into `develop()`**

Add the parameter next to `review_panel`:

```python
    review_panel: int = 1,
    review_lenses: list[str] | None = None,
```

Add to the docstring, after the `review_panel` paragraph:

```
    review_lenses names one dimension per reviewer instead of running K
    reviewers on the same prompt — the panel's disagreement then comes from
    different questions rather than from sampling noise. It takes precedence
    over review_panel (the CLI refuses both, but a library caller passing
    both must not silently get the product of the two).
```

Immediately before the review dispatch, resolve which panel is being asked for:

```python
        panel_lenses = list(review_lenses) if review_lenses else ([""] * review_panel if review_panel > 1 else [])
```

Replace the `if review_panel > 1:` dispatch condition with `if panel_lenses:`, and pass `panel_lenses` where `review_panel` was passed.

The speculative-review gate must skip for a lens panel too — it exists to overlap ONE reviewer with the gate. Change its condition from `review_panel <= 1` to `not (review_panel > 1 or review_lenses)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest tests.test_review_lenses -v`
Expected: all tests through Task 3 PASS

- [ ] **Step 5: Run the full suite — it must be green again**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest discover -s tests`
Expected: OK, including `tests/test_review_panel.py` unchanged. Task 2 knowingly left the numeric callers broken; this is where that is repaired, so a remaining failure there is a real defect, not expected fallout.

- [ ] **Step 6: Commit**

```bash
git add src/shepherd_dev/supervisor.py tests/test_review_lenses.py
git commit -m "feat: develop() runs a lens panel when one is named"
```

---

### Task 4: `--review-lens` on the CLI, and the menu table

**Files:**
- Modify: `src/shepherd_dev/cli.py`
- Modify: `src/shepherd_dev/menu.py`
- Test: `tests/test_review_lenses.py` (append)

**Interfaces:**
- Consumes: `LENS_NAMES` (Task 1); `develop(..., review_lenses=...)` (Task 3).
- Produces: `MAX_REVIEW_LENSES` (int), `_validate_review_lenses(lenses: list[str], *, no_review: bool, provider: str, best_of: int, explicit_panel: int | None) -> str | None`. Nothing later consumes these — Task 5 is docs.

The menu classification is in THIS task, not a later one: `tests/test_menu.py::test_every_flag_is_classified` compares the table against the parser, so adding the flag without classifying it turns the suite red.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_lenses.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest tests.test_review_lenses -v`
Expected: FAIL — argparse rejects `--review-lens`, and `_validate_review_lenses` does not exist.

- [ ] **Step 3: Add the constant and the validator**

In `src/shepherd_dev/cli.py`, next to `MAX_REVIEW_PANEL`:

```python
#: Same bounded-allowance reasoning as MAX_REVIEW_PANEL, and the catalogue
#: is this size anyway — naming every lens is the widest panel on offer.
MAX_REVIEW_LENSES = 5
```

Next to `_validate_review_panel_best_of`:

```python
def _validate_review_lenses(
    lenses: list[str],
    *,
    no_review: bool,
    provider: str,
    best_of: int,
    explicit_panel: int | None,
) -> str | None:
    """None = usable; otherwise the reason to refuse, ready to print.

    An empty list is always usable — that is the default, and this feature
    is opt-in per run.
    """
    from .prompts import LENS_NAMES

    if not lenses:
        return None
    unknown = [name for name in lenses if name not in LENS_NAMES]
    if unknown:
        return (
            f"unknown --review-lens {', '.join(sorted(set(unknown)))} — "
            f"valid lenses: {', '.join(LENS_NAMES)}"
        )
    if len(set(lenses)) != len(lenses):
        # Two reviewers with the same lens is the correlated-sampling
        # problem this feature exists to fix, bought at twice the price.
        return "--review-lens repeats a lens; each may be named once"
    if len(lenses) > MAX_REVIEW_LENSES:
        return f"--review-lens is capped at {MAX_REVIEW_LENSES}"
    if no_review:
        return "--review-lens needs the reviewer (drop --no-review)"
    if provider == "static":
        return "--review-lens needs a reviewing provider (not static)"
    if best_of > 1:
        return "--review-lens does not combine with --best-of"
    if explicit_panel is not None and explicit_panel > 1:
        return "--review-lens does not combine with --review-panel (both are panels)"
    return None
```

- [ ] **Step 4: Add the flag and wire it**

In the `p_run` argparse block, immediately after `--review-panel`:

```python
    p_run.add_argument(
        "--review-lens",
        action="append",
        default=[],
        metavar="NAME",
        help="run one reviewer per named lens instead of one generalist "
             "(repeatable; correctness, security, scope, conventions, tests). "
             "Off unless named on this run",
    )
```

Do NOT add `choices=` — see Global Constraints; the menu drift test cannot classify an append action that also carries choices.

In `_cmd_run_inner`, beside the existing panel validation (after `explicit_panel` is captured):

```python
    bad_lenses = _validate_review_lenses(
        args.review_lens, no_review=args.no_review, provider=args.provider,
        best_of=args.best_of, explicit_panel=explicit_panel,
    )
    if bad_lenses:
        print(f"error: {bad_lenses}", file=sys.stderr)
        return 2
```

And in the `develop(...)` call, after `review_panel=args.review_panel,`:

```python
            review_lenses=args.review_lens,
```

- [ ] **Step 5: Classify the flag in the menu table**

In `src/shepherd_dev/menu.py`, add to `OPTIONS["run"]`, next to the other review entries:

```python
        Opt(dest="review_lens", kind="list", tier=MAIN, flag="--review-lens"),
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest tests.test_review_lenses tests.test_menu -v`
Expected: all PASS, including the menu drift tests.

- [ ] **Step 7: Run the full suite**

Run: `PYTHONPATH="$(pwd)/src" /Users/andrefogelman/shepherd/.venv/bin/python3 -m unittest discover -s tests`
Expected: OK.

- [ ] **Step 8: Commit**

```bash
git add src/shepherd_dev/cli.py src/shepherd_dev/menu.py tests/test_review_lenses.py
git commit -m "feat: add --review-lens to shepherd-dev run"
```

---

### Task 5: Docs

**Files:**
- Modify: `docs/MANUAL.en.md`, `docs/MANUAL.md`

**Interfaces:**
- Consumes: nothing. Produces: nothing.

- [ ] **Step 1: Find the `--review-panel` paragraph in each manual**

Run: `grep -n "review-panel" docs/MANUAL.md docs/MANUAL.en.md`

- [ ] **Step 2: Add the section after it**

In `docs/MANUAL.en.md`, immediately after the `--review-panel` paragraph:

```markdown
**`--review-lens NAME`** puts one reviewer on each named dimension instead of
several reviewers on the same prompt. Repeat it: `--review-lens correctness
--review-lens security`. The lenses are `correctness`, `security`, `scope`,
`conventions` and `tests` — the dimensions the single reviewer already weighs
all at once.

This is why it is worth the tokens: `--review-panel 3` runs the same prompt
three times, so the three samples share whatever that prompt is blind to.
Naming lenses asks three different questions instead, and approval still
requires all of them, so a defect only the security lens would notice still
blocks the proposal.

Off unless you name a lens on that run — there is no saved default, and
`init` does not set one. It does not combine with `--review-panel` (both are
panels) or `--best-of`; either combination is refused rather than silently
ignored.
```

Translate the same section into Portuguese for `docs/MANUAL.md`, matching that file's tone and its established vocabulary (`portão`, `revisor`, `achado`, `ledger de achados`). Keep the lens names in English — they are the literal flag values.

- [ ] **Step 3: Commit**

```bash
git add docs/MANUAL.md docs/MANUAL.en.md
git commit -m "docs: document --review-lens"
```

---

## Self-Review

**Requirement coverage:**

| Requirement | Task |
|---|---|
| Lens-differentiated reviewers | 1 (catalogue + prompt), 2 (dispatch) |
| Opt-in per run, signalled each time | 4 (`default=[]`, no config read, no init default) |
| Default: does not run | 3 (`review_lenses=None` → today's single reviewer), 4 (empty list always usable) |
| Unanimity preserved | 2 (unchanged `_aggregate_review_verdicts`) |
| Refuses to combine with `--review-panel` | 4 (`_validate_review_lenses`) |

**Placeholder scan:** none — every step carries real code. The one judgement call left to the implementer (Task 1 Step 2, how to read the wrapped task's signature) is explicitly framed as a call to make and report, not a gap to fill in.

**Type consistency:** `REVIEW_LENSES: dict[str, str]` and `LENS_NAMES: tuple[str, ...]` are defined in Task 1 and used under those names in Tasks 2 and 4. `run_review_panel`'s third parameter is `lenses: list[str]` in Tasks 2 and 3. `develop(..., review_lenses: list[str] | None = None)` is defined in Task 3 and called with that keyword in Task 4. The argparse dest is `review_lens` (argparse derives it from the flag) throughout Tasks 4's CLI wiring, menu entry, and tests — note it is singular, unlike the `review_lenses` parameter it feeds.

**One risk I could not remove:** Task 2 deliberately leaves the suite red between commits, because changing `run_review_panel`'s signature breaks `develop()` until Task 3 repairs it. The alternative — a compatibility shim accepting both an int and a list — would be dead code the moment Task 3 landed. Task 2's Step 6 says the failure is expected and names what it looks like, and Task 3's Step 5 says a remaining failure there is a real defect.

---

**Plan complete and saved to `docs/superpowers/plans/2026-08-06-review-lenses.md`.** Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
