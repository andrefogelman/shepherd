# Durable Review Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `shepherd-dev run --review-report FILE` writes a durable markdown file — the reviewer's verdict, its issues, the full cross-round ledger history, and the actual proposed diff — instead of that content existing only in the CLI's stdout, which disappears once whatever captured it is gone.

**Architecture:** `develop()` already computes everything a review report needs and holds it in the returned `DevReport` (`.review.summary`/`.issues`, `.ledger`, `.entries`, `.final_run_ref`) — this is a pure rendering + file-write problem, not a new data-collection problem. No `events.ndjson` parsing, no re-fetching a run by ref after the fact (which the caller found NOT reliably possible once a run output is consumed — see the "why not events.ndjson" note below). A small refactor extracts the "turn content entries into `=== FILE: ... ===` reviewer-readable text" logic already living inside `build_diff_text` (`src/shepherd_dev/supervisor.py:752`) into a standalone, reusable function, so the new report writer builds its diff section the exact same way the reviewer itself saw it — not a second, possibly-inconsistent implementation.

**Why not extract from `events.ndjson` after the fact:** `events.ndjson` (`~/.shepherd-dev/runs/<run_id>/events.ndjson`, `src/shepherd_dev/events.py:43-44`) is durable, but its `attempt.diff` event only ever carried `{"files": [...], "run_ref": ...}` (`src/shepherd_dev/supervisor.py:1315`) — the list of changed filenames, never the diff content. Reconstructing the content post-hoc would mean looking the run back up by `run_ref` via `workspace.runs.outputs(run_ref=...)` (the same lookup `settle_run` uses, `src/shepherd_dev/cli.py`) — but a run's output is consume-once (`state != "unconsumed"` after settle/reject, per `settle_run`'s own check), so a report generated after settlement may find nothing left to read. Building the report inline, in the same process, at the moment `develop()` returns — before anything gets consumed — sidesteps that entirely.

**Tech Stack:** Python 3.11+, stdlib `unittest`, this repo's existing `DevReport`/`ReviewVerdict`/`Ledger` types.

## Global Constraints

- `--review-report` is added to the `run` subcommand only — not `run2`/`runN`/`best-of` — mirroring how `--review-rounds` and `--review-panel` are also `run`-only today. No task in this plan touches `run2`/`runN`.
- The flag must work regardless of `--json` (the report file writes either way) and regardless of whether the run succeeded, was rejected, or errored — a `DevReport` for a FAILED run is still worth a durable record (what was attempted, why it stopped). No task may make report-writing conditional on `report.succeeded`.
- No change to `build_diff_text`'s existing behavior or callers (`run_review`) — the refactor in Task 1 must be behavior-preserving; the existing test suite covering `run_review`'s diff text is the regression guard.
- The report file write is best-effort: a failure to write it (bad path, permission) must print a warning to stderr and must NOT change the command's exit code or otherwise affect the run's own success/failure reporting — this is a side artifact, not a critical path.

---

### Task 1: Extract a shared entries-to-diff-text renderer

**Files:**
- Modify: `src/shepherd_dev/supervisor.py:752-761` (`build_diff_text`)
- Test: `tests/test_supervisor.py` (append; check the file exists first — if not, create it following the `sys.path.insert`/`from __future__ import annotations` header convention used throughout this repo's other `tests/test_*.py` files, e.g. `tests/test_review_panel.py:1-20`)

**Interfaces:**
- Consumes: nothing new — this is a refactor of existing code.
- Produces: `_render_entries_as_diff_text(entries, limit: int = DIFF_TEXT_LIMIT) -> str` — a module-level function in `src/shepherd_dev/supervisor.py`, taking anything with a dict-like `.items()` yielding `(str, bytes)` pairs (a plain `dict[str, bytes]`, or the `Entries` type). Used by Task 2's report renderer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_supervisor.py (create if it doesn't exist — check first)
"""Tests for shared supervisor.py helpers not covered by their own test
file. Runnable with: python -m unittest tests.test_supervisor
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.supervisor import (  # noqa: E402
    _render_entries_as_diff_text,
    build_diff_text,
)


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
        class _Changeset:
            def __init__(self, files):
                self._files = files

            @property
            def changed_paths(self):
                return list(self._files)

            def read_file(self, rel):
                b = self._files.get(rel)
                return (b, 0o644) if b is not None else None

        text = build_diff_text(_Changeset({"a.py": b"A = 1\n"}))
        self.assertIn("=== FILE: a.py (proposed content) ===", text)
        self.assertIn("A = 1", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_supervisor -v`
Expected: FAIL with `ImportError: cannot import name '_render_entries_as_diff_text'`

- [ ] **Step 3: Refactor the implementation**

In `src/shepherd_dev/supervisor.py`, replace the current `build_diff_text` (lines 752-761):

```python
def build_diff_text(changeset, limit: int = DIFF_TEXT_LIMIT) -> str:
    """Render a retained changeset's content entries as reviewer-readable text."""
    parts: list[str] = []
    for rel, content in read_changeset_entries(changeset).items():
        text = content.decode("utf-8", errors="replace")
        parts.append(f"=== FILE: {rel} (proposed content) ===\n{text}")
    diff = "\n\n".join(parts)
    if len(diff) > limit:
        diff = diff[:limit] + f"\n\n[... truncated at {limit} chars ...]"
    return diff
```

with:

```python
def _render_entries_as_diff_text(entries, limit: int = DIFF_TEXT_LIMIT) -> str:
    """Render already-extracted content entries (rel path -> bytes) as the
    same reviewer-readable `=== FILE: ... ===` text build_diff_text has
    always produced — factored out so a second caller (the review-report
    writer) renders a diff identically to what the reviewer itself saw,
    rather than reimplementing this formatting a second time."""
    parts: list[str] = []
    for rel, content in entries.items():
        text = content.decode("utf-8", errors="replace")
        parts.append(f"=== FILE: {rel} (proposed content) ===\n{text}")
    diff = "\n\n".join(parts)
    if len(diff) > limit:
        diff = diff[:limit] + f"\n\n[... truncated at {limit} chars ...]"
    return diff


def build_diff_text(changeset, limit: int = DIFF_TEXT_LIMIT) -> str:
    """Render a retained changeset's content entries as reviewer-readable text."""
    return _render_entries_as_diff_text(read_changeset_entries(changeset), limit)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_supervisor -v`
Expected: all 3 tests PASS

- [ ] **Step 5: Run the full existing suite to confirm the refactor is behavior-preserving**

Run: `python -m unittest discover -s tests`
Expected: OK, same count as before this task (this refactor touches code every existing `run_review`-related test already exercises — a regression here would show up as an existing failure, not a new one)

- [ ] **Step 6: Commit**

```bash
git add src/shepherd_dev/supervisor.py tests/test_supervisor.py
git commit -m "refactor: extract _render_entries_as_diff_text from build_diff_text"
```

---

### Task 2: `render_review_report` — pure DevReport-to-markdown renderer

**Files:**
- Modify: `src/shepherd_dev/supervisor.py` (add after `DevReport.summary()`, i.e. after line ~149, before `materialize_into`)
- Test: `tests/test_supervisor.py` (append)

**Interfaces:**
- Consumes: `DevReport`, `ReviewVerdict`, `Ledger` (all existing, unchanged); `_render_entries_as_diff_text` (Task 1).
- Produces: `render_review_report(report: DevReport) -> str` — used by Task 3's CLI wiring. Builds its own diff text internally from `report.entries` when present; the caller passes only the report.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_supervisor.py`:

```python
class RenderReviewReportTests(unittest.TestCase):
    def _report(self, *, succeeded=True, review=None, entries=None, blocked_reason=None):
        from shepherd_dev.supervisor import Attempt, DevReport, GateResult

        report = DevReport(feature="add CPF validation", succeeded=succeeded, repo="/r")
        report.final_run_ref = "run-abc" if succeeded else None
        report.attempts = [
            Attempt(1, "run-abc", ["validators.py"], [], GateResult(True, 0, "ok"), "passed", duration_s=3.2)
        ]
        report.entries = entries
        report.review = review
        report.blocked_reason = blocked_reason
        return report

    def test_includes_feature_and_outcome(self):
        from shepherd_dev.supervisor import render_review_report

        text = render_review_report(self._report())
        self.assertIn("add CPF validation", text)
        self.assertIn("passed_unreviewed", text)  # no .review set

    def test_includes_verdict_summary_and_issues(self):
        from shepherd_dev.supervisor import ReviewVerdict, render_review_report

        report = self._report(
            review=ReviewVerdict(approved=False, summary="Two problems found.", issues=["missing null check", "off-by-one"])
        )
        text = render_review_report(report)
        self.assertIn("REJECTED", text)
        self.assertIn("Two problems found.", text)
        self.assertIn("missing null check", text)
        self.assertIn("off-by-one", text)

    def test_includes_the_diff_when_entries_are_present(self):
        from shepherd_dev.supervisor import render_review_report

        report = self._report(entries={"validators.py": b"def validate_cpf(s): ...\n"})
        text = render_review_report(report)
        self.assertIn("=== FILE: validators.py (proposed content) ===", text)
        self.assertIn("def validate_cpf", text)

    def test_omits_a_diff_section_when_there_are_no_entries(self):
        """A failed run (no passing proposal) still gets a report — just
        without a diff section, since there is no content to show."""
        from shepherd_dev.supervisor import render_review_report

        report = self._report(succeeded=False, entries=None)
        text = render_review_report(report)
        self.assertNotIn("=== FILE:", text)

    def test_includes_the_ledger_when_present(self):
        from shepherd_dev.supervisor import Ledger, render_review_report

        report = self._report()
        report.ledger = Ledger()
        report.ledger.record_round(1, ["cache is never invalidated"])
        text = render_review_report(report)
        self.assertIn("cache is never invalidated", text)

    def test_includes_the_blocked_reason_when_present(self):
        from shepherd_dev.supervisor import render_review_report

        report = self._report(succeeded=False, blocked_reason="no progress after 3 attempts")
        text = render_review_report(report)
        self.assertIn("no progress after 3 attempts", text)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_supervisor.RenderReviewReportTests -v`
Expected: FAIL with `ImportError: cannot import name 'render_review_report'`

- [ ] **Step 3: Write the implementation**

In `src/shepherd_dev/supervisor.py`, immediately after `DevReport.summary()`'s closing (search for the end of the `summary` method inside the `DevReport` class, right after its `return "\n".join(lines)` — this is a MODULE-LEVEL function, defined right after the `DevReport` class body ends, not a method on it):

```python
def render_review_report(report: DevReport) -> str:
    """Durable markdown record of one develop() run: verdict, issues, the
    full cross-round ledger history, and the actual proposed diff — the
    same content DevReport.summary() prints to stdout, rendered so it
    survives past the process that produced it. Never raises: a
    malformed/partial report still gets a best-effort file rather than an
    empty one.
    """
    lines = [
        f"# Review report: {report.feature}",
        "",
        f"- outcome: `{report.outcome}`",
        f"- succeeded: {report.succeeded}",
    ]
    if report.blocked_reason:
        lines.append(f"- blocked: {report.blocked_reason}")
    if report.final_run_ref:
        lines.append(f"- run ref: `{report.final_run_ref}`")
    lines.append("")

    lines.append("## Attempts")
    lines.append("")
    for a in report.attempts:
        lines.append(f"- attempt {a.number}: run=`{a.run_ref}` verdict={a.verdict} changed={len(a.changed_paths)}")
        if a.error:
            lines.append(f"  - error: {a.error}")
        if a.gate and not a.gate.passed:
            reason = a.gate.infra_error or a.gate.output_tail[-500:]
            lines.append(f"  - gate: exit={a.gate.exit_code} {reason}")
    lines.append("")

    if report.review is not None:
        lines.append("## Review")
        lines.append("")
        if report.review.error:
            lines.append(f"UNAVAILABLE: {report.review.error}")
        else:
            lines.append(f"**{'APPROVED' if report.review.approved else 'REJECTED'}**")
            lines.append("")
            if report.review.summary:
                lines.append(report.review.summary)
            if report.review.issues:
                lines.append("")
                lines.append("Issues:")
                for issue in report.review.issues:
                    lines.append(f"- {issue}")
        lines.append("")

    if report.ledger is not None:
        rendered = report.ledger.render()
        if rendered:
            lines.append("## Findings ledger")
            lines.append("")
            lines.append(rendered)
            lines.append("")

    if report.entries:
        lines.append("## Diff")
        lines.append("")
        lines.append(_render_entries_as_diff_text(report.entries))
        lines.append("")

    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_supervisor -v`
Expected: all tests PASS (Task 1's 3 plus this task's 6)

- [ ] **Step 5: Commit**

```bash
git add src/shepherd_dev/supervisor.py tests/test_supervisor.py
git commit -m "feat: add render_review_report — DevReport as durable markdown"
```

---

### Task 3: `--review-report FILE` on `shepherd-dev run`

**Files:**
- Modify: `src/shepherd_dev/cli.py` (argparse block for `p_run`, and `_cmd_run_inner` right after the `develop(...)` call)
- Test: `tests/test_review_panel.py` or a new `tests/test_review_report_cli.py` — follow whichever convention the codebase favors by the time you implement this (check if `tests/test_supervisor.py` from Tasks 1-2 is the more natural home instead, given it's CLI-level rather than pure-function; a new small file is also fine, this repo has many single-purpose test files)

**Interfaces:**
- Consumes: `render_review_report(report: DevReport) -> str` (Task 2).
- Produces: nothing further consumed by another task — this is the last task with functional code (Task 4 is docs only).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review_report_cli.py
"""Tests for --review-report on `shepherd-dev run`. Runnable with:
python -m unittest tests.test_review_report_cli
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    import shepherd as _sp  # noqa: F401

    _HAS_SUBSTRATE = True
except Exception:
    _HAS_SUBSTRATE = False


class ReviewReportFlagParsingTests(unittest.TestCase):
    def test_flag_defaults_to_none(self):
        from shepherd_dev.cli import build_parser

        args = build_parser().parse_args(["run", "add X"])
        self.assertIsNone(args.review_report)

    def test_flag_accepts_a_path(self):
        from shepherd_dev.cli import build_parser

        args = build_parser().parse_args(["run", "add X", "--review-report", "task-3-review.md"])
        self.assertEqual(args.review_report, "task-3-review.md")


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class ReviewReportCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shepherd-review-report-"))
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.py").write_text("V = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)
        subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli", "init", "--repo", str(self.tmp), "--test-cmd", "true"],
            check=True, capture_output=True, text=True,
        )

    def test_review_report_file_is_written_on_a_run(self):
        out = self.tmp / "review.md"
        result = subprocess.run(
            [
                sys.executable, "-m", "shepherd_dev.cli", "run", "add a comment to a.py",
                "--repo", str(self.tmp), "--provider", "static", "--no-review",
                "--review-report", str(out),
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(out.is_file(), "review report file was not created")
        text = out.read_text()
        self.assertIn("# Review report:", text)
        self.assertIn("add a comment to a.py", text)

    def test_a_bad_review_report_path_warns_but_does_not_fail_the_run(self):
        bad_path = self.tmp / "no" / "such" / "dir" / "review.md"  # parent dirs don't exist
        result = subprocess.run(
            [
                sys.executable, "-m", "shepherd_dev.cli", "run", "add a comment to a.py",
                "--repo", str(self.tmp), "--provider", "static", "--no-review",
                "--review-report", str(bad_path),
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)  # run itself still succeeds
        self.assertIn("review-report", result.stderr.lower())  # a warning was printed


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_review_report_cli -v`
Expected: FAIL — `argparse` rejects `--review-report` as unrecognized

- [ ] **Step 3: Write the implementation**

In `src/shepherd_dev/cli.py`, add the argparse flag to `p_run` — find the block that adds `--review-panel` (added by an earlier plan; search for `"--review-panel"`) and add immediately after it:

```python
    p_run.add_argument(
        "--review-report", default=None, metavar="FILE",
        help="write a durable markdown report (verdict, issues, ledger, diff) to this path",
    )
```

In `_cmd_run_inner`, right after the `develop(...)` call and its `report = develop(...)` assignment (find the `with sp.open(repo_root) as workspace:` block that calls `develop` for the plain `run` path — NOT the `_run_best_of` path, which is out of scope per Global Constraints), immediately after the `with` block closes (after `reporter.close(ok=report.succeeded)`), add:

```python
    if args.review_report:
        from .supervisor import render_review_report

        try:
            Path(args.review_report).write_text(render_review_report(report), encoding="utf-8")
        except Exception as exc:
            print(f"warning: --review-report could not write {args.review_report}: {exc}", file=sys.stderr)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_review_report_cli -v`
Expected: all 4 tests PASS

- [ ] **Step 5: Run the full existing suite**

Run: `python -m unittest discover -s tests`
Expected: OK — no existing test passes `--review-report`, so `args.review_report` resolves to `None` everywhere else and the new `if args.review_report:` block never fires for any pre-existing test.

- [ ] **Step 6: Commit**

```bash
git add src/shepherd_dev/cli.py tests/test_review_report_cli.py
git commit -m "feat: add --review-report to shepherd-dev run"
```

---

### Task 4: Docs

**Files:**
- Modify: `docs/MANUAL.md`, `docs/MANUAL.en.md`

**Interfaces:**
- Consumes: nothing (prose only).
- Produces: nothing consumed by later tasks — this is the last task.

- [ ] **Step 1: Find the `--review-panel` mention in each manual**

Run: `grep -n "review-panel" docs/MANUAL.md docs/MANUAL.en.md`

- [ ] **Step 2: Add a paired `--review-report` paragraph next to it**

In both files, immediately after the existing `--review-panel` explanation, add (translate to Portuguese for `MANUAL.md`, matching that file's tone; keep this English text in `MANUAL.en.md`):

```markdown
`--review-report FILE` writes the run's verdict, issues, findings-ledger
history, and the actual proposed diff to `FILE` as markdown — durable,
independent of however stdout gets captured (or discarded) by whatever
launched the run. Written once, at the end of the run, from the same
in-memory report the console summary is built from — nothing gets
re-fetched or re-derived afterward. Works regardless of outcome: a failed
or blocked run still gets a report, just without a diff section (there is
no passing proposal's content to show).
```

- [ ] **Step 3: Commit**

```bash
git add docs/MANUAL.md docs/MANUAL.en.md
git commit -m "docs: document --review-report"
```

---

## Self-Review

**Spec coverage:**
- "review lives only in events.ndjson (missing summary) and ephemeral stdout" → the earlier, already-shipped fix (not part of this plan) added `summary` to `review.verdict`; THIS plan's `--review-report` goes further, giving a fully durable, complete, ready-to-read artifact per run.
- "attempt.diff guarda o diff proposto" (reported diff storage) → verified against the actual code this claim doesn't hold for `events.ndjson` (file list only, no content) — this plan's architecture note documents why, and sidesteps the problem entirely by building the report inline instead of by post-hoc extraction.
- "extrair verdict+issues+diff, gravar como task-N-review.md" → `--review-report FILE` lets any caller (an SDD-style controller, a script, a human) name the file `task-N-review.md` themselves; shepherd-dev stays agnostic to any particular numbering scheme, consistent with this codebase's stated "shepherd knows nothing about any service/toolchain" design philosophy (`src/shepherd_dev/remotegate.py`'s module docstring makes the same point about `test_remote` config).

**Placeholder scan:** none — every step has real code, no TBD/"handle edge cases" placeholders.

**Type consistency:** `render_review_report(report: DevReport) -> str` is the name and signature used identically in Task 2 (definition) and Task 3 (the CLI call site) — no renames across tasks. `_render_entries_as_diff_text(entries, limit)` likewise matches between Task 1 (definition) and Task 2 (its one call site inside `render_review_report`).

---

**Plan complete and saved to `docs/2026-08-05-durable-review-report.md`.** Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
