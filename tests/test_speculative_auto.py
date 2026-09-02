"""The reviewer overlaps the gate when the repo's own track record says the
gate will pass — flag over config over the history's verdict.

Runnable with: python -m unittest tests.test_speculative_auto
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.history import gate_pass_rate  # noqa: E402


def _run(repo: str, verdicts: list[str], provider: str = "claude") -> dict:
    return {
        "kind": "run", "provider": provider, "repo": repo,
        "attempts": [{"verdict": v} for v in verdicts],
    }


class GatePassRate(unittest.TestCase):
    def test_counts_only_gate_verdicts_of_this_repos_claude_runs(self):
        repo = Path(tempfile.mkdtemp(prefix="shepherd-spec-"))
        events = [
            _run(str(repo), ["tests_failed", "passed"]),
            _run(str(repo), ["run_failed", "policy_rejected", "passed"]),
            _run("/elsewhere", ["tests_failed", "tests_failed"]),
            _run(str(repo), ["passed"], provider="static"),
        ]
        rate, judged = gate_pass_rate(repo, events=events)
        self.assertEqual(judged, 3)
        self.assertAlmostEqual(rate, 2 / 3)

    def test_fewer_than_three_judged_attempts_is_no_track_record(self):
        repo = Path(tempfile.mkdtemp(prefix="shepherd-spec-"))
        self.assertIsNone(gate_pass_rate(repo, events=[_run(str(repo), ["passed", "passed"])]))
        self.assertIsNone(gate_pass_rate(repo, events=[]))

    def test_only_the_most_recent_runs_count(self):
        repo = Path(tempfile.mkdtemp(prefix="shepherd-spec-"))
        old = [_run(str(repo), ["tests_failed"]) for _ in range(20)]
        new = [_run(str(repo), ["passed"]) for _ in range(5)]
        rate, judged = gate_pass_rate(repo, last_n=5, events=old + new)
        self.assertEqual((rate, judged), (1.0, 5))


class Resolution(unittest.TestCase):
    def _resolve(self, *, explicit=None, setting="auto", rate=None, no_review=False, provider="claude"):
        from unittest.mock import patch

        from shepherd_dev import cli, config, history

        args = SimpleNamespace(speculative_review=explicit, no_review=no_review, provider=provider)
        with patch.object(config, "speculative_review_config", return_value=setting), \
                patch.object(history, "gate_pass_rate", return_value=rate):
            return cli._resolve_speculative_review(args, Path("/r"))

    def test_the_flag_wins_both_ways(self):
        self.assertTrue(self._resolve(explicit=True, setting="off"))
        self.assertFalse(self._resolve(explicit=False, setting="on", rate=(1.0, 9)))

    def test_config_on_and_off(self):
        self.assertTrue(self._resolve(setting="on"))
        self.assertFalse(self._resolve(setting="off", rate=(1.0, 9)))

    def test_auto_follows_the_track_record(self):
        self.assertTrue(self._resolve(rate=(0.8, 10)))
        self.assertFalse(self._resolve(rate=(0.5, 10)))
        self.assertFalse(self._resolve(rate=None))

    def test_auto_never_overlaps_without_a_reviewer(self):
        self.assertFalse(self._resolve(rate=(1.0, 10), no_review=True))
        self.assertFalse(self._resolve(rate=(1.0, 10), provider="static"))

    def test_the_parser_is_tri_state(self):
        from shepherd_dev.cli import build_parser

        parser = build_parser()
        self.assertIsNone(parser.parse_args(["run", "f"]).speculative_review)
        self.assertTrue(parser.parse_args(["run", "f", "--speculative-review"]).speculative_review)
        self.assertFalse(parser.parse_args(["run", "f", "--no-speculative-review"]).speculative_review)
        self.assertFalse(parser.parse_args(["run2", "a", "b", "--no-speculative-review"]).speculative_review)

    def test_config_values(self):
        from unittest.mock import patch

        from shepherd_dev import config

        repo = Path(tempfile.mkdtemp(prefix="shepherd-spec-cfg-"))
        with patch.object(config, "GLOBAL_CONFIG", repo / "no-global.json"):
            self.assertEqual(config.speculative_review_config(repo), "auto")
            config.save_config(repo, {"speculative_review": True})
            self.assertEqual(config.speculative_review_config(repo), "on")
            config.save_config(repo, {"speculative_review": "OFF"})
            self.assertEqual(config.speculative_review_config(repo), "off")
            config.save_config(repo, {"speculative_review": "sometimes"})
            self.assertEqual(config.speculative_review_config(repo), "auto")  # an unknown value is no setting


if __name__ == "__main__":
    unittest.main()
