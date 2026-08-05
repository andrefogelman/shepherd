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
