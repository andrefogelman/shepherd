# Adversarial Review Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `shepherd-dev run` judge a proposal with K independent reviewers instead of one, so a real problem only one of K sees still blocks — while every existing single-reviewer run stays byte-for-byte unchanged, because the feature is opt-in and asked once at `init` time.

**Architecture:** A new `run_review_panel()` in `supervisor.py` clones the repo K times (reusing `parallel.py`'s existing worker-clone machinery — `_clone_many`), runs the existing `run_review()` once per clone in parallel, and folds the K verdicts into one via a new pure function `_aggregate_review_verdicts()` (unanimous approval, union of issues/resolved). `develop()` gains a `review_panel: int = 1` parameter; at `1` it takes the exact code path it takes today. The CLI adds `--review-panel` to `run` only (mirroring `--review-rounds`, which is also `run`-only today) and resolves it as: explicit flag > the repo's saved `.shepherd-dev.json` choice > `1`. `shepherd-dev init` asks the question interactively (EOF/non-interactive-safe, default `1` — today's behavior) and persists the answer.

**Tech Stack:** Python 3.11+, stdlib `unittest` + `concurrent.futures.ThreadPoolExecutor`, the `shepherd` (shepherd-ai) substrate already used throughout this repo.

## Global Constraints

- Default behavior must not change: `review_panel` defaults to `1` at every layer (argparse, config resolver, `develop()` signature) and `1` must take the IDENTICAL code path `develop()` takes today (calls `run_review()` directly, not `run_review_panel()`).
- Scope is `shepherd-dev run` only — NOT `run2`/`runN`/`best-of`. `--review-rounds` itself is `run`-only today (confirmed: only `p_run` defines it); this feature follows the same precedent. Extending to the parallel commands is explicitly out of scope for this plan.
- The init-time question must be interactive-safe: EOF or a non-interactive stdin (CI, a pipe) must fall through to the default (`1`) rather than hang or crash — mirror the existing `_ask_decision()` pattern in `cli.py:373-386`.
- No changes to `ledger.py` — `Ledger.record_round(number, issues, resolved, approved)` already accepts one merged call per round; the aggregation happens before that call, not inside the ledger.
- Cap panel size the same way `--review-rounds` is capped (`MAX_REVIEW_ROUNDS = 5` at `cli.py:50`): add `MAX_REVIEW_PANEL = 5` next to it.

---

### Task 1: `_aggregate_review_verdicts` — pure merge of K verdicts into one

**Files:**
- Modify: `src/shepherd_dev/supervisor.py` (add near `run_review`, after line 840)
- Test: `tests/test_review_panel.py` (new file)

**Interfaces:**
- Consumes: `ReviewVerdict` dataclass (`src/shepherd_dev/supervisor.py:59-66`, fields: `approved: bool`, `summary: str`, `issues: list[str]`, `resolved: list[str]`, `error: str | None`)
- Produces: `_aggregate_review_verdicts(verdicts: list[ReviewVerdict]) -> ReviewVerdict` — used by Task 2's `run_review_panel`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_review_panel.py
"""Tests for the adversarial review panel (K independent reviewers instead
of 1). Runnable with: python -m unittest tests.test_review_panel
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.supervisor import ReviewVerdict, _aggregate_review_verdicts  # noqa: E402


class AggregateReviewVerdictsTests(unittest.TestCase):
    def test_unanimous_approval_is_approved(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="a", issues=[], resolved=[]),
            ReviewVerdict(approved=True, summary="b", issues=[], resolved=[]),
        ])
        self.assertTrue(v.approved)

    def test_a_single_rejection_blocks_the_whole_panel(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="a", issues=[], resolved=[]),
            ReviewVerdict(approved=False, summary="b", issues=["found a bug"], resolved=[]),
        ])
        self.assertFalse(v.approved)

    def test_issues_are_unioned_across_reviewers_deduped(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=False, summary="a", issues=["issue A", "issue B"], resolved=[]),
            ReviewVerdict(approved=False, summary="b", issues=["issue B", "issue C"], resolved=[]),
        ])
        self.assertEqual(v.issues, ["issue A", "issue B", "issue C"])

    def test_resolved_ids_are_unioned_across_reviewers_deduped(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="a", issues=[], resolved=["id1", "id2"]),
            ReviewVerdict(approved=True, summary="b", issues=[], resolved=["id2", "id3"]),
        ])
        self.assertEqual(v.resolved, ["id1", "id2", "id3"])

    def test_any_reviewer_error_makes_the_whole_panel_an_error(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="a", issues=[], resolved=[]),
            ReviewVerdict(approved=False, summary="", error="review run failed: boom"),
        ])
        self.assertFalse(v.approved)
        self.assertIsNotNone(v.error)
        self.assertIn("boom", v.error)

    def test_empty_panel_is_an_error_not_a_silent_approval(self):
        v = _aggregate_review_verdicts([])
        self.assertFalse(v.approved)
        self.assertIsNotNone(v.error)

    def test_summary_credits_every_reviewer(self):
        v = _aggregate_review_verdicts([
            ReviewVerdict(approved=True, summary="looks fine", issues=[], resolved=[]),
            ReviewVerdict(approved=True, summary="also fine", issues=[], resolved=[]),
        ])
        self.assertIn("looks fine", v.summary)
        self.assertIn("also fine", v.summary)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_review_panel -v`
Expected: FAIL with `ImportError: cannot import name '_aggregate_review_verdicts'`

- [ ] **Step 3: Write the implementation**

In `src/shepherd_dev/supervisor.py`, immediately after the `run_review` function (after line 839, before the `_TEST_FILE_RE` block at line 842):

```python
def _aggregate_review_verdicts(verdicts: list[ReviewVerdict]) -> ReviewVerdict:
    """Combine K independent reviewers' verdicts into one.

    Unanimous approval: a panel exists so that a real problem only ONE lens
    catches still blocks — outvoting it would defeat the point. Issues and
    resolved ids are unioned (order-preserving, deduped by exact text/id;
    the same normalize-and-hash dedup the Ledger does later handles two
    reviewers phrasing the same problem differently). Any reviewer error
    (an infra failure, a malformed REVIEW.json) makes the whole panel an
    error, same as a single reviewer's error already does today.
    """
    if not verdicts:
        return ReviewVerdict(approved=False, summary="", error="review panel produced no verdicts")
    errors = [v.error for v in verdicts if v.error]
    if errors:
        return ReviewVerdict(approved=False, summary="", error="; ".join(errors))

    issues: list[str] = []
    seen_issues: set[str] = set()
    for v in verdicts:
        for issue in v.issues:
            if issue not in seen_issues:
                seen_issues.add(issue)
                issues.append(issue)

    resolved: list[str] = []
    seen_resolved: set[str] = set()
    for v in verdicts:
        for fid in v.resolved:
            if fid not in seen_resolved:
                seen_resolved.add(fid)
                resolved.append(fid)

    summaries = [
        f"[reviewer {i + 1}/{len(verdicts)}] {v.summary}" for i, v in enumerate(verdicts) if v.summary
    ]
    return ReviewVerdict(
        approved=all(v.approved for v in verdicts),
        summary="\n".join(summaries),
        issues=issues,
        resolved=resolved,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_review_panel -v`
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/shepherd_dev/supervisor.py tests/test_review_panel.py
git commit -m "feat: add _aggregate_review_verdicts for the adversarial review panel"
```

---

### Task 2: `run_review_panel` — clone K reviewers, run them in parallel, aggregate

**Files:**
- Modify: `src/shepherd_dev/supervisor.py` (add after `_aggregate_review_verdicts`, before `_TEST_FILE_RE`)
- Test: `tests/test_review_panel.py` (append)

**Interfaces:**
- Consumes: `_aggregate_review_verdicts` (Task 1); `run_review` (`src/shepherd_dev/supervisor.py:764`, unchanged signature); `_clone_many(repo_root: Path, n: int) -> list[Path]` (`src/shepherd_dev/parallel.py:116`, unchanged, imported LOCALLY inside the function — `supervisor.py` currently has zero substrate coupling, `parallel.py` already breaks this exact same cycle the other direction with a local `from .supervisor import fast_copytree` at `parallel.py:99`, so mirror that).
- Produces: `run_review_panel(repo_root: Path, review_task, size: int, *, feature: str, changeset=None, diff_text: str | None = None, provider: str = "claude", placement: str = "jail", context_pack: str | None = None, findings: str = "") -> ReviewVerdict` — used by Task 3's `develop()` wiring.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_review_panel.py`:

```python
try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class RunReviewPanelTests(unittest.TestCase):
    """Real clones (via the same _clone_many parallel workers already use),
    real sp.open per clone — only the reviewer's own AI call is faked, same
    boundary LocalGateStageTests/SpeculativeReviewTests already fake at."""

    def setUp(self):
        import subprocess
        import tempfile

        from shepherd_dev import supervisor as S

        self.repo = Path(tempfile.mkdtemp(prefix="shepherd-panel-"))
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("V = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli", "init", "--repo", str(self.repo)],
            check=True, capture_output=True, text=True,
        )
        self._orig_run_review = S.run_review

    def tearDown(self):
        from shepherd_dev import supervisor as S

        S.run_review = self._orig_run_review

    def test_unanimous_approval_from_three_independent_clones(self):
        from shepherd_dev import supervisor as S

        calls = []

        def _fake_review(workspace, review_task, **kw):
            calls.append(kw.get("feature"))
            return S.ReviewVerdict(approved=True, summary="fine", issues=[], resolved=[])

        S.run_review = _fake_review
        verdict = S.run_review_panel(
            self.repo, object(), 3, feature="add X", diff_text="+V = 2\n",
        )
        self.assertTrue(verdict.approved)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(f == "add X" for f in calls))

    def test_one_dissenter_blocks_and_its_issue_survives(self):
        from shepherd_dev import supervisor as S

        n = {"i": 0}

        def _fake_review(workspace, review_task, **kw):
            n["i"] += 1
            if n["i"] == 2:
                return S.ReviewVerdict(approved=False, summary="found it", issues=["real bug"], resolved=[])
            return S.ReviewVerdict(approved=True, summary="fine", issues=[], resolved=[])

        S.run_review = _fake_review
        verdict = S.run_review_panel(self.repo, object(), 3, feature="add X")
        self.assertFalse(verdict.approved)
        self.assertIn("real bug", verdict.issues)

    def test_clones_are_cleaned_up_after_the_panel_runs(self):
        import tempfile as _tempfile

        from shepherd_dev import supervisor as S

        before = set(Path(_tempfile.gettempdir()).glob("shepherd-par-*"))
        S.run_review = lambda workspace, review_task, **kw: S.ReviewVerdict(
            approved=True, summary="x", issues=[], resolved=[]
        )
        S.run_review_panel(self.repo, object(), 2, feature="add X")
        after = set(Path(_tempfile.gettempdir()).glob("shepherd-par-*"))
        self.assertEqual(before, after)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_review_panel.RunReviewPanelTests -v`
Expected: FAIL with `AttributeError: module 'shepherd_dev.supervisor' has no attribute 'run_review_panel'`

- [ ] **Step 3: Write the implementation**

In `src/shepherd_dev/supervisor.py`, immediately after `_aggregate_review_verdicts`:

```python
def run_review_panel(
    repo_root: Path,
    review_task,
    size: int,
    *,
    feature: str,
    changeset=None,
    diff_text: str | None = None,
    provider: str = "claude",
    placement: str = "jail",
    context_pack: str | None = None,
    findings: str = "",
) -> ReviewVerdict:
    """Run `size` independent reviewers in separate clones, aggregate into
    one verdict via _aggregate_review_verdicts.

    run_review's own docstring explains why a single reviewer runs in the
    caller's existing workspace lane: v0.2 lane limits require disjoint
    roots for concurrent workspace.run calls. A panel of `size` concurrent
    reviewers needs `size` disjoint roots, so each gets its own clone — the
    exact machinery parallel.py already uses to isolate concurrent workers
    (_clone_many), reused here rather than reinvented.
    """
    import shutil
    from concurrent.futures import ThreadPoolExecutor

    import shepherd as sp

    from .parallel import _clone_many

    if diff_text is None:
        diff_text = build_diff_text(changeset)

    clones = _clone_many(repo_root, size)
    try:
        def _one(clone: Path) -> ReviewVerdict:
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
                )

        with ThreadPoolExecutor(max_workers=size) as pool:
            verdicts = list(pool.map(_one, clones))
    finally:
        for clone in clones:
            shutil.rmtree(clone.parent, ignore_errors=True)
    return _aggregate_review_verdicts(verdicts)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_review_panel -v`
Expected: all tests PASS (the 7 from Task 1 plus the 3 new ones — `RunReviewPanelTests` needs the real substrate, present in this repo's `.venv`)

- [ ] **Step 5: Commit**

```bash
git add src/shepherd_dev/supervisor.py tests/test_review_panel.py
git commit -m "feat: add run_review_panel — K independent reviewers in parallel clones"
```

---

### Task 3: Wire `review_panel` into `develop()`

**Files:**
- Modify: `src/shepherd_dev/supervisor.py:1050-1092` (signature + docstring), `:1257-1263` (gate the speculative-review shortcut), `:1350-1361` (branch to the panel)
- Test: `tests/test_review_panel.py` (append)

**Interfaces:**
- Consumes: `run_review_panel` (Task 2, imported/called by plain module-level name — tests fake it the same way `test_review_rounds.py`'s `_run` helper fakes `sup.run_review`).
- Produces: `develop(..., review_panel: int = 1, ...)` — used by Task 4's CLI wiring.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_panel.py`:

```python
@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class DevelopReviewPanelWiringTests(unittest.TestCase):
    """develop() driven by fakes — same harness style as
    test_review_rounds.py's DevelopReworkLoopTests._run, extended with a
    review_panel arg and a fake for run_review_panel specifically (so these
    tests check ROUTING, not the panel's own clone/aggregate mechanics —
    those are Task 1/2's job)."""

    def _run(self, *, review_panel, panel_verdict=None, gate_passes=None):
        from shepherd_dev import supervisor as sup

        calls = {"worker": 0, "review": 0, "panel": 0, "panel_size": None, "gates": []}

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
                calls["worker"] += 1
                return _Run()

        def _read_entries(changeset):
            return dict(changeset)

        def _gate(repo_root, entries, test_cmd, timeout, **kw):
            i = len(calls["gates"])
            passed = True if gate_passes is None else gate_passes[i]
            calls["gates"].append(passed)
            return sup.GateResult(passed, 0 if passed else 1, "gate output")

        def _review(workspace, review_task, **kw):
            calls["review"] += 1
            return sup.ReviewVerdict(approved=True, summary="s", issues=[], resolved=[])

        def _review_panel(repo_root, review_task, size, **kw):
            calls["panel"] += 1
            calls["panel_size"] = size
            return panel_verdict or sup.ReviewVerdict(approved=True, summary="p", issues=[], resolved=[])

        orig = (
            sup.read_changeset_entries, sup._run_gate, sup.run_review,
            sup.run_review_panel, sup._start_gate_warmup,
        )
        sup.read_changeset_entries = _read_entries
        sup._run_gate = _gate
        sup.run_review = _review
        sup.run_review_panel = _review_panel
        sup._start_gate_warmup = lambda *a, **k: None
        try:
            report = sup.develop(
                _Workspace(), object(), repo=object(), repo_root=Path("/r"),
                feature="add X", test_cmd="pytest -q", review_task=object(),
                max_attempts=1, review_panel=review_panel,
            )
        finally:
            (
                sup.read_changeset_entries, sup._run_gate, sup.run_review,
                sup.run_review_panel, sup._start_gate_warmup,
            ) = orig
        return report, calls

    def test_panel_size_one_calls_run_review_not_the_panel(self):
        _, calls = self._run(review_panel=1)
        self.assertEqual(calls["review"], 1)
        self.assertEqual(calls["panel"], 0)

    def test_panel_size_above_one_calls_the_panel_not_run_review(self):
        _, calls = self._run(review_panel=3)
        self.assertEqual(calls["review"], 0)
        self.assertEqual(calls["panel"], 1)
        self.assertEqual(calls["panel_size"], 3)

    def test_the_panels_verdict_is_the_reports_verdict(self):
        from shepherd_dev import supervisor as sup

        v = sup.ReviewVerdict(approved=False, summary="p", issues=["x"], resolved=[])
        report, _ = self._run(review_panel=2, panel_verdict=v)
        self.assertIs(report.review, v)

    def test_default_review_panel_is_one(self):
        import inspect

        from shepherd_dev import supervisor as sup

        self.assertEqual(inspect.signature(sup.develop).parameters["review_panel"].default, 1)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_review_panel.DevelopReviewPanelWiringTests -v`
Expected: FAIL — `develop()` raises `TypeError: develop() got an unexpected keyword argument 'review_panel'` (and `test_default_review_panel_is_one` fails with `KeyError: 'review_panel'`)

- [ ] **Step 3: Write the implementation**

In `src/shepherd_dev/supervisor.py`, `develop()`'s signature (starts at line 1050) — add the new parameter next to `review_rounds`:

```python
    review_rounds: int = 1,
    review_panel: int = 1,
```

In the docstring (after the existing `review_rounds > 1 lets a REJECTED-but-passing...` paragraph, around line 1090), add:

```
    review_panel > 1 replaces the single reviewer with that many independent
    ones (separate clones, run in parallel) — approval requires all of them
    to agree; a real problem only one catches still blocks. 1 (default)
    takes the exact path today's single-reviewer runs already take.
```

At line 1263, gate the speculative-review shortcut to panel size 1 (a speculative single review makes no sense once there are several; the ordinary post-gate path below already handles `review_panel > 1` correctly):

```python
        if test_cmd is not None and review_task is not None and speculative_review and review_panel <= 1:
```

At lines 1350-1361, branch on `review_panel`:

```python
        verdict = _reap_spec()  # already ran overlapped with the gate
        if verdict is None:
            if review_panel > 1:
                verdict = run_review_panel(
                    repo_root,
                    review_task,
                    review_panel,
                    feature=feature,
                    changeset=changeset,
                    provider=provider,
                    placement=placement,
                    context_pack=context_pack,
                    findings=ledger.guidance() if ledger is not None else "",
                )
            else:
                verdict = run_review(
                    workspace,
                    review_task,
                    feature=feature,
                    changeset=changeset,
                    provider=provider,
                    placement=placement,
                    context_pack=context_pack,
                    findings=ledger.guidance() if ledger is not None else "",
                )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_review_panel -v`
Expected: all tests PASS (Task 1 + Task 2 + these 4 new ones)

- [ ] **Step 5: Run the full existing suite to confirm nothing regressed**

Run: `python -m unittest discover -s tests`
Expected: OK, same count as before plus the new tests — in particular `tests/test_review_rounds.py` and `tests/test_perf.py::SpeculativeReviewTests` must still pass unchanged (they never pass `review_panel`, so it defaults to `1` and takes today's path).

- [ ] **Step 6: Commit**

```bash
git add src/shepherd_dev/supervisor.py tests/test_review_panel.py
git commit -m "feat: wire review_panel into develop() (default 1 = unchanged today)"
```

---

### Task 4: CLI — `--review-panel` flag, validation, config resolution

**Files:**
- Modify: `src/shepherd_dev/cli.py` (constant near line 50, validator near line 69, argparse near line 1919-1922, resolution + `develop()` call in `_cmd_run_inner` near lines 802-808 and 897-917)
- Test: `tests/test_review_panel.py` (append)

**Interfaces:**
- Consumes: `develop(..., review_panel=...)` (Task 3); `config.load_config(repo_root) -> dict` (`src/shepherd_dev/config.py:30`, unchanged).
- Produces: `MAX_REVIEW_PANEL` (int constant), `_validate_review_panel(size: int, *, no_review: bool, provider: str) -> str | None`, `_resolve_review_panel(repo_root: Path, explicit: int | None) -> int` — the last one is also what Task 5's `cmd_init` reads back to show the current saved value.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_panel.py`:

```python
@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class ReviewPanelCliTests(unittest.TestCase):
    def test_flag_defaults_to_none_not_one(self):
        # None, not 1: this is how _resolve_review_panel tells "not passed"
        # apart from "explicitly passed 1" — same trick --test-cmd already
        # uses (cli.py's p_run.add_argument("--test-cmd", default=None, ...)).
        from shepherd_dev.cli import build_parser

        args = build_parser().parse_args(["run", "add X"])
        self.assertIsNone(args.review_panel)

    def test_flag_is_accepted_up_to_the_cap(self):
        from shepherd_dev.cli import MAX_REVIEW_PANEL, build_parser

        self.assertEqual(MAX_REVIEW_PANEL, 5)
        args = build_parser().parse_args(["run", "add X", "--review-panel", "5"])
        self.assertEqual(args.review_panel, 5)

    def test_above_the_cap_is_refused(self):
        from shepherd_dev.cli import _validate_review_panel

        self.assertIsNotNone(_validate_review_panel(6, no_review=False, provider="claude"))
        self.assertIsNone(_validate_review_panel(5, no_review=False, provider="claude"))

    def test_below_one_is_refused(self):
        from shepherd_dev.cli import _validate_review_panel

        self.assertIsNotNone(_validate_review_panel(0, no_review=False, provider="claude"))

    def test_panel_without_a_reviewer_is_refused(self):
        from shepherd_dev.cli import _validate_review_panel

        self.assertIsNotNone(_validate_review_panel(2, no_review=True, provider="claude"))
        self.assertIsNotNone(_validate_review_panel(2, no_review=False, provider="static"))
        # one reviewer is the status quo — must stay legal everywhere
        self.assertIsNone(_validate_review_panel(1, no_review=True, provider="static"))

    def test_resolve_prefers_explicit_over_saved_over_default(self):
        import tempfile

        from shepherd_dev import config
        from shepherd_dev.cli import _resolve_review_panel

        repo = Path(tempfile.mkdtemp(prefix="shepherd-panel-resolve-"))
        self.assertEqual(_resolve_review_panel(repo, None), 1)  # no config, no flag
        config.save_config(repo, {"review_panel": 3})
        self.assertEqual(_resolve_review_panel(repo, None), 3)  # saved config wins over default
        self.assertEqual(_resolve_review_panel(repo, 2), 2)  # explicit flag wins over saved config
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_review_panel.ReviewPanelCliTests -v`
Expected: FAIL — `argparse` rejects `--review-panel` as unrecognized, and `_validate_review_panel`/`_resolve_review_panel` don't exist yet

- [ ] **Step 3: Write the implementation**

In `src/shepherd_dev/cli.py`, next to `MAX_REVIEW_ROUNDS` (after line 50):

```python
#: Same reasoning as MAX_REVIEW_ROUNDS: a bounded, pre-approved allowance,
#: not an open-ended amplifier.
MAX_REVIEW_PANEL = 5
```

Next to `_validate_review_rounds` (after line 69, before the blank line at 70):

```python


def _validate_review_panel(size: int, *, no_review: bool, provider: str) -> str | None:
    """None = usable; otherwise the reason to refuse, ready to print."""
    if size < 1:
        return "--review-panel must be at least 1"
    if size > MAX_REVIEW_PANEL:
        return f"--review-panel is capped at {MAX_REVIEW_PANEL} (got {size})"
    if size > 1 and no_review:
        return "--review-panel > 1 needs the reviewer (drop --no-review)"
    if size > 1 and provider == "static":
        return "--review-panel > 1 needs a reviewing provider (not static)"
    return None


def _resolve_review_panel(repo_root: Path, explicit: int | None) -> int:
    """Explicit --review-panel wins; else the repo's saved init-time choice
    (config.py's `review_panel` key); else 1 — today's single-reviewer
    behavior, unchanged."""
    if explicit is not None:
        return explicit
    saved = config.load_config(repo_root).get("review_panel")
    return saved if isinstance(saved, int) and saved >= 1 else 1
```

In the argparse block for `p_run`, right after the `--review-rounds` block (after line 1922, before `--gate-timeout` at line 1923):

```python
    p_run.add_argument(
        "--review-panel", type=int, default=None,
        help=f"K independent reviewers instead of 1 — approval needs unanimity "
             f"(default: the repo's saved init-time choice, else 1; max {MAX_REVIEW_PANEL})",
    )
```

In `_cmd_run_inner`, right after the existing `bad_rounds` check (after line 808, before the `best_of` block at line 810):

```python
    args.review_panel = _resolve_review_panel(repo_root, args.review_panel)
    bad_panel = _validate_review_panel(
        args.review_panel, no_review=args.no_review, provider=args.provider,
    )
    if bad_panel:
        print(f"error: {bad_panel}", file=sys.stderr)
        return 2
```

In the `develop(...)` call inside `_cmd_run_inner` (after `review_rounds=args.review_rounds,` at line 907):

```python
            review_panel=args.review_panel,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_review_panel -v`
Expected: all tests PASS

- [ ] **Step 5: Run the full existing suite**

Run: `python -m unittest discover -s tests`
Expected: OK — `tests/test_review_rounds.py::ReviewRoundsCliTests` and every existing `cmd_run`/`_cmd_run_inner` test must still pass unchanged (`args.review_panel` resolves to `1` when nothing sets it, `develop()` gets `review_panel=1`, identical to before this task existed).

- [ ] **Step 6: Commit**

```bash
git add src/shepherd_dev/cli.py tests/test_review_panel.py
git commit -m "feat: add --review-panel to shepherd-dev run"
```

---

### Task 5: `shepherd-dev init` asks the question, once, with a safe default

**Files:**
- Modify: `src/shepherd_dev/cli.py:1546-1607` (`cmd_init`), `:2052-2056` (`p_init` argparse), `src/shepherd_dev/config.py:1-6` (module docstring)
- Test: `tests/test_review_panel.py` (append)

**Interfaces:**
- Consumes: `_validate_review_panel`, `MAX_REVIEW_PANEL`, `config.save_config` (all from Task 4 / existing).
- Produces: `_ask_review_panel(default: int = 1) -> int` — the interactive prompt, EOF/non-interactive-safe.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_panel.py`:

```python
class AskReviewPanelTests(unittest.TestCase):
    """No substrate needed — this is a pure input()-wrapping function."""

    def test_empty_answer_keeps_the_default(self):
        from unittest.mock import patch

        from shepherd_dev.cli import _ask_review_panel

        with patch("builtins.input", return_value=""):
            self.assertEqual(_ask_review_panel(default=1), 1)

    def test_eof_keeps_the_default(self):
        from unittest.mock import patch

        from shepherd_dev.cli import _ask_review_panel

        with patch("builtins.input", side_effect=EOFError):
            self.assertEqual(_ask_review_panel(default=1), 1)

    def test_a_valid_number_is_used(self):
        from unittest.mock import patch

        from shepherd_dev.cli import _ask_review_panel

        with patch("builtins.input", return_value="3"):
            self.assertEqual(_ask_review_panel(default=1), 3)

    def test_garbage_input_keeps_the_default(self):
        from unittest.mock import patch

        from shepherd_dev.cli import _ask_review_panel

        with patch("builtins.input", return_value="banana"):
            self.assertEqual(_ask_review_panel(default=1), 1)

    def test_out_of_range_keeps_the_default(self):
        from unittest.mock import patch

        from shepherd_dev.cli import _ask_review_panel

        with patch("builtins.input", return_value="99"):
            self.assertEqual(_ask_review_panel(default=1), 1)


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class InitPersistsReviewPanelTests(unittest.TestCase):
    def test_explicit_flag_skips_the_prompt_and_saves(self):
        import subprocess
        import tempfile

        from shepherd_dev import config

        repo = Path(tempfile.mkdtemp(prefix="shepherd-init-panel-"))
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        result = subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli", "init", "--repo", str(repo), "--review-panel", "3"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.load_config(repo).get("review_panel"), 3)

    def test_no_flag_and_no_stdin_saves_the_default(self):
        import subprocess
        import tempfile

        from shepherd_dev import config

        repo = Path(tempfile.mkdtemp(prefix="shepherd-init-panel-"))
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        result = subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli", "init", "--repo", str(repo)],
            input="", capture_output=True, text=True,  # empty stdin => EOF on input()
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.load_config(repo).get("review_panel"), 1)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_review_panel.AskReviewPanelTests tests.test_review_panel.InitPersistsReviewPanelTests -v`
Expected: FAIL — `_ask_review_panel` does not exist; `init` rejects `--review-panel` as unrecognized; saved config has no `review_panel` key

- [ ] **Step 3: Write the implementation**

In `src/shepherd_dev/cli.py`, next to `_ask_decision` (after line 386, before `_interactive_settle_run`):

```python
def _ask_review_panel(default: int = 1) -> int:
    """Ask how many independent reviewers judge each proposal at init time.
    Empty answer, EOF (non-interactive stdin), or anything unparsable/out of
    range keeps `default` — today's single-reviewer behavior."""
    try:
        ans = input(
            f"\nReview panel size — independent reviewers per proposal, "
            f"unanimous approval required [{default}, max {MAX_REVIEW_PANEL}]: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not ans:
        return default
    try:
        n = int(ans)
    except ValueError:
        return default
    if n < 1 or n > MAX_REVIEW_PANEL:
        return default
    return n
```

In `cmd_init`, right after the existing `test_cmd` handling block (after line 1606, before `return 0` at line 1607):

```python

    if args.review_panel is not None:
        bad = _validate_review_panel(args.review_panel, no_review=False, provider="claude")
        if bad:
            print(f"error: {bad}", file=sys.stderr)
            return 2
        panel = args.review_panel
    else:
        panel = _ask_review_panel(default=1)
    config.save_config(repo_root, {"review_panel": panel})
    if panel == 1:
        print(f"review panel: 1 (single reviewer)  →  {config.CONFIG_NAME}")
    else:
        print(f"review panel: {panel} independent reviewers, unanimous approval  →  {config.CONFIG_NAME}")
```

In the `p_init` argparse block (after line 2054, before line 2055's `p_init.set_defaults`):

```python
    p_init.add_argument(
        "--review-panel", type=int, default=None,
        help=f"save this many independent reviewers without asking interactively "
             f"(max {MAX_REVIEW_PANEL}); omit to be asked",
    )
```

In `src/shepherd_dev/config.py`, update the module docstring (lines 4-5):

```python
Config lives at <repo>/.shepherd-dev.json (committed by default — it is project
metadata, not local state). Stores `test_cmd` and `review_panel` today.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_review_panel -v`
Expected: all tests PASS (every test added across Tasks 1-5)

- [ ] **Step 5: Run the full existing suite one more time**

Run: `python -m unittest discover -s tests`
Expected: OK, full green — in particular no existing `init` test (there are none today, but any future scripted/CI use of `shepherd-dev init` with closed/empty stdin) must still exit 0, never hang.

- [ ] **Step 6: Commit**

```bash
git add src/shepherd_dev/cli.py src/shepherd_dev/config.py tests/test_review_panel.py
git commit -m "feat: ask review-panel size at init time, default 1 (today's behavior)"
```

---

### Task 6: Docs

**Files:**
- Modify: `docs/MANUAL.md`, `docs/MANUAL.en.md`

**Interfaces:**
- Consumes: nothing (prose only).
- Produces: nothing consumed by later tasks — this is the last task.

- [ ] **Step 1: Find the `--review-rounds` mention in each manual**

Run: `grep -n "review-rounds\|review_rounds" docs/MANUAL.md docs/MANUAL.en.md`

- [ ] **Step 2: Add a paired `--review-panel` paragraph next to it**

In both files, immediately after the existing `--review-rounds` explanation, add (translate the Portuguese for `MANUAL.md`, keep English for `MANUAL.en.md`):

```markdown
`--review-panel K` replaces the single reviewer with K independent ones,
each in its own clone, run in parallel — approval requires all K to agree.
A real problem only one of them catches still blocks the proposal. Default
is 1 (today's single-reviewer behavior) unless the repo was `init`ed with a
different choice, or `--review-panel` is passed explicitly. Cost is K× the
review tokens per round; wall-clock stays close to a single review since
the K reviewers run concurrently.
```

- [ ] **Step 3: Commit**

```bash
git add docs/MANUAL.md docs/MANUAL.en.md
git commit -m "docs: document --review-panel"
```

---

## Self-Review

**Spec coverage:**
- "estude e proponha como opção" (study + propose) → this plan, offered before any code changes (still awaiting the execution go-ahead below).
- "perguntado no momento de inicializar" (asked at init time) → Task 5 (`_ask_review_panel` wired into `cmd_init`).
- "default como está agora" (default = current behavior) → enforced at every layer: `develop(review_panel=1)` takes today's exact `run_review` call (Task 3); CLI resolver falls back to `1` with no saved config and no flag (Task 4); `_ask_review_panel`'s every failure/skip path returns the passed-in `default=1` (Task 5).

**Placeholder scan:** none — every step has real code, no TBD/"handle edge cases"/"similar to Task N" placeholders.

**Type consistency:** `review_panel: int` is the name and type used identically in `develop()` (Task 3), `_resolve_review_panel`/`_validate_review_panel`/argparse (Task 4), and `_ask_review_panel`/`config.save_config`'s `{"review_panel": ...}` key (Task 5) — no renames across tasks.

---

**Plan complete and saved to `docs/2026-08-04-adversarial-review-panel.md`.** Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
