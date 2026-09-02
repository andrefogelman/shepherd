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

    def test_a_file_too_big_for_the_budget_says_so_in_place(self):
        """The old form cut the whole rendering at `limit` and appended one
        marker at the very end, so any file past the cut vanished without a
        trace. The trim is now per file and named."""
        text = _render_entries_as_diff_text({"big.py": b"x" * 500}, limit=200)
        self.assertIn("big.py", text)
        self.assertIn("not shown", text)
        self.assertIn("do not approve", text)

    def test_every_changed_path_is_listed_however_tight_the_budget(self):
        """The reported failure: nine files changed, the reviewer received
        two, and nothing told it the other seven existed. Paths are cheap —
        the scope is the one thing that must never be dropped."""
        entries = {f"lib/{'a' if i < 2 else 'z'}/file{i}.ex": b"x" * 40_000 for i in range(9)}
        text = _render_entries_as_diff_text(entries, limit=1_000)
        for rel in entries:
            self.assertIn(rel, text, f"{rel} vanished from the manifest")
        self.assertIn("CHANGED FILES (9)", text)

    def test_a_small_file_is_not_starved_by_a_huge_one(self):
        """Alphabetical order used to decide who got seen: a flat cut let the
        first files eat the whole budget. A small file now always fits."""
        entries = {"a_huge.py": b"x" * 50_000, "z_small.py": b"print(1)\n"}
        text = _render_entries_as_diff_text(entries, limit=2_000)
        self.assertIn("print(1)", text, "the small file's content must survive")
        self.assertIn("not shown", text, "the huge one is the one that gets trimmed")

    def test_an_empty_changeset_still_renders_a_manifest(self):
        self.assertIn("CHANGED FILES (0)", _render_entries_as_diff_text({}))

    def test_the_prompt_tells_the_reviewer_what_a_trimmed_body_means(self):
        """The renderer marking a gap is only half of it — the reviewer has
        to be told that a marked file must not be approved on that evidence,
        or it can still sign off on a change it only partly read."""
        from shepherd_dev.prompts import get_prompt

        prompt = " ".join(get_prompt("review").split())
        self.assertIn("CHANGED FILES", prompt)
        self.assertIn("not shown", prompt)
        # The proposal is now applied in the reviewer's working copy, so the
        # instruction is to read the trimmed file THERE — and still never to
        # approve a file it did not fully read.
        self.assertIn("open the file in the working directory", prompt)
        self.assertIn("not one you may approve", prompt)

    def test_the_prompt_forbids_reading_absence_out_of_the_worktree(self):
        """Observed on a real run: the reviewer could not find a new
        LiveView in the tree, went looking, saw the PRE-change tree, and
        reported the file as nonexistent — while the gate was green on a
        suite that could not compile without it. Saying the tree is
        pre-change was not enough; the inference itself has to be banned."""
        from shepherd_dev.prompts import get_prompt

        # Collapsed, so the assertion survives the paragraph being rewrapped.
        # Since the proposal is applied into the working copy before the
        # reviewer starts, the ban is stated the other way round: every
        # listed path IS there, and absence may not be concluded from a
        # search — the inference itself stays banned.
        prompt = " ".join(get_prompt("review").split())
        self.assertIn("exists in the working directory with its proposed content", prompt)
        self.assertIn('"does not exist"', prompt)
        self.assertIn("never a reason to reject", prompt)


class UnifiedDiffTests(unittest.TestCase):
    """With a repo_root the reviewer gets a diff, not whole files. A 48KB
    file whose change is twenty lines used to cost 48KB of the budget."""

    def _repo(self):
        from tmpdirs import mkdtemp

        repo = Path(mkdtemp(prefix="shepherd-diff-"))
        (repo / "big.py").write_text("".join(f"line {i}\n" for i in range(2000)))
        return repo

    def test_an_edited_file_is_rendered_as_a_diff(self):
        repo = self._repo()
        after = (repo / "big.py").read_text().replace("line 5\n", "line 5 CHANGED\n")

        text = _render_entries_as_diff_text({"big.py": after.encode()}, repo_root=repo)
        self.assertIn("=== FILE: big.py (unified diff) ===", text)
        self.assertIn("+line 5 CHANGED", text)
        self.assertNotIn("line 1999", text, "untouched lines must not be shipped")

    def test_the_diff_is_a_fraction_of_the_whole_file(self):
        repo = self._repo()
        after = (repo / "big.py").read_text().replace("line 5\n", "line 5 CHANGED\n")

        as_diff = _render_entries_as_diff_text({"big.py": after.encode()}, repo_root=repo)
        as_full = _render_entries_as_diff_text({"big.py": after.encode()})
        self.assertLess(len(as_diff), len(as_full) // 10)

    def test_a_new_file_has_no_before_so_it_ships_whole(self):
        repo = self._repo()
        text = _render_entries_as_diff_text({"brand_new.py": b"X = 1\n"}, repo_root=repo)
        self.assertIn("=== FILE: brand_new.py (new file) ===", text)
        self.assertIn("X = 1", text)

    def test_without_a_repo_root_the_old_whole_file_form_is_kept(self):
        text = _render_entries_as_diff_text({"a.py": b"A = 1\n"})
        self.assertIn("=== FILE: a.py (proposed content) ===", text)

    def test_a_file_the_worker_left_untouched_is_marked_not_dumped(self):
        """The changeset can carry a file whose content equals the worktree's.
        Shipping it whole would spend budget saying nothing."""
        repo = self._repo()
        same = (repo / "big.py").read_bytes()
        text = _render_entries_as_diff_text({"big.py": same}, repo_root=repo)
        self.assertIn("=== FILE: big.py (unchanged) ===", text)
        self.assertLess(len(text), 200)

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

    def test_policy_violations_reach_the_report(self):
        """summary() renders policy_violations; render_review_report must
        not be a downgrade for the one failure class where stdout names
        WHICH rule was violated (final review, Important #1)."""
        from shepherd_dev.supervisor import Attempt, render_review_report

        report = self._report(succeeded=False)
        report.attempts = [
            Attempt(1, "run-xyz", ["etc/passwd"], ["path escapes allowed prefixes"], None, "policy_rejected")
        ]
        text = render_review_report(report)
        self.assertIn("path escapes allowed prefixes", text)

    def test_diff_content_is_fenced_against_the_reports_own_structure(self):
        """A proposal file containing '## Diff' and a ``` fence must not be
        able to forge the report's own section boundaries (final review,
        Important #2)."""
        from shepherd_dev.supervisor import render_review_report

        tricky = "# My Project\n\n## Diff\n\n```bash\nnpm i\n```\n"
        report = self._report(entries={"README.md": tricky.encode()})
        text = render_review_report(report)
        lines = text.splitlines()

        # the report's own "## Diff" heading is the first occurrence; it is
        # immediately followed by a blank line and an opening fence longer
        # than any backtick run inside the embedded content.
        heading = lines.index("## Diff")
        self.assertEqual(lines[heading + 1], "")
        opener = lines[heading + 2]
        self.assertRegex(opener, r"^````+text$")
        closer = opener.removesuffix("text")
        closer_idx = lines.index(closer, heading + 3)

        # everything the proposal contributed — including its own "## Diff"
        # line and its own ``` fence, which are too short to close ours —
        # sits strictly inside the fence.
        body_lines = lines[heading + 3:closer_idx]
        self.assertIn("## Diff", body_lines)
        self.assertIn("```bash", body_lines)

    def test_gate_tail_is_fenced_against_the_reports_own_structure(self):
        """Multi-line gate output containing '## Attempts' and a ``` fence
        must not be able to forge the report's own structure either (final
        review, Important #2)."""
        from shepherd_dev.supervisor import Attempt, GateResult, render_review_report

        tricky_output = "## Attempts\n\n```\nsome pytest output\n```\n"
        report = self._report(succeeded=False)
        report.attempts = [
            Attempt(1, "run-xyz", ["a.py"], [], GateResult(False, 1, tricky_output), "tests_failed")
        ]
        text = render_review_report(report)
        lines = text.splitlines()

        gate_idx = next(i for i, ln in enumerate(lines) if ln.strip().startswith("- gate: exit=1"))
        opener = lines[gate_idx + 1].strip()
        self.assertRegex(opener, r"^````+text$")
        closer = opener.removesuffix("text")
        closer_idx = next(i for i in range(gate_idx + 2, len(lines)) if lines[i].strip() == closer)

        body_lines = [lines[i].strip() for i in range(gate_idx + 2, closer_idx)]
        self.assertIn("## Attempts", body_lines)
        self.assertIn("```", body_lines)

    def test_review_summary_is_fenced_against_the_reports_own_structure(self):
        """The reviewer's own free prose lands at column 0, so a line of it
        starting with '#' would forge one of the report's section headings."""
        from shepherd_dev.supervisor import ReviewVerdict, render_review_report

        tricky = "Looks fine overall.\n\n## Findings ledger\n\nforged section\n"
        report = self._report(review=ReviewVerdict(approved=True, summary=tricky, issues=[]))
        text = render_review_report(report)
        lines = text.splitlines()

        opener = next(ln for ln in lines if ln.startswith("```") and ln.endswith("text"))
        self.assertRegex(opener, r"^```+text$")
        opener_idx = lines.index(opener)
        closer = opener.removesuffix("text")
        closer_idx = lines.index(closer, opener_idx + 1)

        # the reviewer's own "## Findings ledger" line sits inside the fence,
        # so it cannot be mistaken for the report's real ledger section
        self.assertIn("## Findings ledger", lines[opener_idx + 1:closer_idx])

    def test_a_single_line_issue_stays_a_real_list_item(self):
        """A one-line issue cannot forge anything — the "- " in front of it
        means no markdown block construct starts at column 0 — so it keeps
        rendering as a list item rather than being needlessly fenced."""
        from shepherd_dev.supervisor import ReviewVerdict, render_review_report

        report = self._report(
            review=ReviewVerdict(approved=False, summary="", issues=["missing null check"])
        )
        text = render_review_report(report)
        self.assertIn("- missing null check", text.splitlines())

    def test_a_multi_line_issue_is_fenced(self):
        """Past its first line a multi-line issue IS at column 0, so it needs
        the fence. Indenting alone would not do: an ATX heading indented
        inside a list item still renders as a heading, just a nested one."""
        from shepherd_dev.supervisor import ReviewVerdict, render_review_report

        tricky = "the cache is stale\n## Review\nforged section"
        report = self._report(review=ReviewVerdict(approved=False, summary="", issues=[tricky]))
        text = render_review_report(report)
        lines = [ln.strip() for ln in text.splitlines()]

        opener = next(ln for ln in lines if ln.startswith("```") and ln.endswith("text"))
        opener_idx = lines.index(opener)
        closer = opener.removesuffix("text")
        closer_idx = lines.index(closer, opener_idx + 1)

        body = lines[opener_idx + 1:closer_idx]
        self.assertIn("the cache is stale", body)
        self.assertIn("## Review", body)  # forged heading is inside the fence


class RendererDriftTests(unittest.TestCase):
    """DevReport.summary() (stdout) and render_review_report() (the durable
    file) are two hand-written renderers over the same data, and they HAVE
    already drifted: render_review_report shipped without `policy_violations`,
    which summary() renders, so a policy_rejected run's permanent record said
    strictly less than the stdout it exists to replace.

    These tests are the guard for that whole class. Each field of `Attempt`
    and `ReviewVerdict` is either given a sentinel that must appear in BOTH
    renderings, or listed as deliberately-unrendered with a reason. A field
    added later belongs to neither set, which fails `test_*_fields_are_all_
    classified` — forcing the decision instead of letting the next field slip
    into one renderer only.
    """

    #: Attempt fields whose value must survive into both renderings.
    ATTEMPT_SENTINELS = {
        "run_ref": "run-SENTINELREF",
        "verdict": "policy_rejected",
        "error": "SENTINELERROR worker blew up",
        "policy_violations": "SENTINELPOLICY .env is out of scope",
    }
    #: Attempt fields neither renderer emits the value of, and why.
    ATTEMPT_NOT_RENDERED = {
        "number": "rendered as a position ('attempt 1'), not as a searchable value",
        "changed_paths": "both render len(), deliberately — the paths are in the diff section",
        "gate": "composite; its exit_code and tail are covered by GATE_TAIL_SENTINEL below",
        "duration_s": "telemetry for the history file, not part of either human-facing report",
        "usage": (
            "a dict, rendered as derived numbers (events.format_usage) by BOTH "
            "renderers — pinned by tests/test_telemetry.py, not by a string sentinel"
        ),
    }
    #: ReviewVerdict fields whose value must survive into both renderings.
    VERDICT_SENTINELS = {
        "summary": "SENTINELSUMMARY the change is unsound",
        "issues": "SENTINELISSUE missing null check",
        "advisories": "SENTINELADVISORY prefer defp here",
        "error": "SENTINELVERDICTERROR reviewer produced no REVIEW.json",
    }
    #: ReviewVerdict fields neither renderer emits the value of, and why.
    VERDICT_NOT_RENDERED = {
        "approved": "rendered as the APPROVED/REJECTED word, not as a searchable value",
        "resolved": "ledger bookkeeping — the ledger section reports the resulting states",
        "advisory": (
            "KNOWN GAP, not drift: neither renderer surfaces it, so an unread "
            "(heuristic) verdict prints as a bare APPROVED. Only reachable via "
            "the hosted providers, which do not reach these renderers today."
        ),
        "usage": (
            "a dict, rendered as derived numbers (events.format_usage) by BOTH "
            "renderers — pinned by tests/test_telemetry.py, not by a string sentinel"
        ),
    }
    GATE_TAIL_SENTINEL = "SENTINELGATE assert 1 == 2"

    def _both_renderings(self, report):
        from shepherd_dev.supervisor import render_review_report

        return report.summary(), render_review_report(report)

    def _fully_populated(self, *, verdict_error: bool):
        from shepherd_dev.supervisor import (
            Attempt,
            DevReport,
            GateResult,
            Ledger,
            ReviewVerdict,
        )

        report = DevReport(feature="add X", succeeded=False, repo="/r")
        report.attempts = [
            Attempt(
                number=7,
                run_ref=self.ATTEMPT_SENTINELS["run_ref"],
                changed_paths=["a.py"],
                policy_violations=[self.ATTEMPT_SENTINELS["policy_violations"]],
                gate=GateResult(False, 1, self.GATE_TAIL_SENTINEL),
                verdict=self.ATTEMPT_SENTINELS["verdict"],
                error=self.ATTEMPT_SENTINELS["error"],
                duration_s=3.2,
            )
        ]
        # error and summary/issues are mutually exclusive in both renderers
        # (an unavailable verdict has no prose), so each needs its own report.
        if verdict_error:
            report.review = ReviewVerdict(
                approved=False, summary="", error=self.VERDICT_SENTINELS["error"]
            )
        else:
            report.review = ReviewVerdict(
                approved=False,
                summary=self.VERDICT_SENTINELS["summary"],
                issues=[self.VERDICT_SENTINELS["issues"]],
                advisories=[self.VERDICT_SENTINELS["advisories"]],
            )
        report.ledger = Ledger()
        report.ledger.record_round(1, ["SENTINELFINDING cache never invalidated"])
        return report

    def test_attempt_fields_are_all_classified(self):
        import dataclasses

        from shepherd_dev.supervisor import Attempt

        classified = set(self.ATTEMPT_SENTINELS) | set(self.ATTEMPT_NOT_RENDERED)
        self.assertEqual(
            classified,
            {f.name for f in dataclasses.fields(Attempt)},
            "a new Attempt field must be given a sentinel (rendered in BOTH "
            "summary() and render_review_report) or listed in "
            "ATTEMPT_NOT_RENDERED with a reason",
        )

    def test_review_verdict_fields_are_all_classified(self):
        import dataclasses

        from shepherd_dev.supervisor import ReviewVerdict

        classified = set(self.VERDICT_SENTINELS) | set(self.VERDICT_NOT_RENDERED)
        self.assertEqual(
            classified,
            {f.name for f in dataclasses.fields(ReviewVerdict)},
            "a new ReviewVerdict field must be given a sentinel (rendered in "
            "BOTH summary() and render_review_report) or listed in "
            "VERDICT_NOT_RENDERED with a reason",
        )

    def test_every_attempt_sentinel_reaches_both_renderers(self):
        stdout, durable = self._both_renderings(self._fully_populated(verdict_error=False))
        for field, sentinel in self.ATTEMPT_SENTINELS.items():
            self.assertIn(sentinel, stdout, f"summary() dropped Attempt.{field}")
            self.assertIn(sentinel, durable, f"render_review_report dropped Attempt.{field}")

    def test_the_gate_tail_reaches_both_renderers(self):
        stdout, durable = self._both_renderings(self._fully_populated(verdict_error=False))
        self.assertIn(self.GATE_TAIL_SENTINEL, stdout)
        self.assertIn(self.GATE_TAIL_SENTINEL, durable)

    def test_verdict_prose_sentinels_reach_both_renderers(self):
        stdout, durable = self._both_renderings(self._fully_populated(verdict_error=False))
        for field in ("summary", "issues"):
            sentinel = self.VERDICT_SENTINELS[field]
            self.assertIn(sentinel, stdout, f"summary() dropped ReviewVerdict.{field}")
            self.assertIn(sentinel, durable, f"render_review_report dropped ReviewVerdict.{field}")

    def test_verdict_error_reaches_both_renderers(self):
        stdout, durable = self._both_renderings(self._fully_populated(verdict_error=True))
        sentinel = self.VERDICT_SENTINELS["error"]
        self.assertIn(sentinel, stdout)
        self.assertIn(sentinel, durable)

    def test_the_ledger_reaches_both_renderers(self):
        stdout, durable = self._both_renderings(self._fully_populated(verdict_error=False))
        self.assertIn("SENTINELFINDING cache never invalidated", stdout)
        self.assertIn("SENTINELFINDING cache never invalidated", durable)


if __name__ == "__main__":
    unittest.main()
