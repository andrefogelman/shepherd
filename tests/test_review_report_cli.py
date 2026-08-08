# tests/test_review_report_cli.py
"""Tests for --review-report on `shepherd-dev run`. Runnable with:
python -m unittest tests.test_review_report_cli
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import isolate_runs_dir, mkdtemp  # noqa: E402


def setUpModule() -> None:
    """These tests shell out to the CLI, which writes a run log. Keep those
    out of ~/.shepherd-dev/runs — the developer's real history."""
    isolate_runs_dir()

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
        self.tmp = Path(mkdtemp(prefix="shepherd-review-report-"))
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.py").write_text("V = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)
        subprocess.run(
            [sys.executable, "-m", "shepherd_dev.cli", "init", "--repo", str(self.tmp), "--test-cmd", "true"],
            # input="": see test_review_panel — init prompts, and an inherited
            # stdin that never closes hangs the suite.
            input="", check=True, capture_output=True, text=True,
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

    def test_review_report_is_a_silent_no_op_no_longer_on_best_of(self):
        """--best-of returns from _cmd_run_inner before the write block ever
        runs, so the flag used to be a silent no-op there (final review,
        Important #3). --best-of 2 --no-review --provider static is the
        cheapest combination that genuinely reaches _run_best_of: best-of
        with a non-static provider requires review for ranking, but the
        static provider is explicitly exempted from that requirement
        (cli.py's `args.best_of > 1 and args.no_review and args.provider
        not in ("static",)` guard), so this needs no model access."""
        out = self.tmp / "review.md"
        result = subprocess.run(
            [
                sys.executable, "-m", "shepherd_dev.cli", "run", "add a comment to a.py",
                "--repo", str(self.tmp), "--provider", "static", "--no-review",
                "--best-of", "2", "--review-report", str(out),
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not written for this run path", result.stderr)
        self.assertFalse(out.exists(), "no file should be written on the best-of path")


if __name__ == "__main__":
    unittest.main()
