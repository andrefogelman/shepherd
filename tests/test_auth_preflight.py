"""The worker's credential is checked, and refreshed if it would not last the
run, before the run spends anything.

Runnable with: python -m unittest tests.test_auth_preflight
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.preflight import (  # noqa: E402
    AUTH_REFRESH_WINDOW_S,
    auth_preflight,
    credential_expires_in,
    quota_reason,
)


def _blob(expires_in_s: float, now: float = 1_700_000_000.0) -> bytes:
    return json.dumps({"claudeAiOauth": {"expiresAt": int((now + expires_in_s) * 1000)}}).encode()


NOW = 1_700_000_000.0


class Expiry(unittest.TestCase):
    def test_reads_the_blobs_expires_at_in_milliseconds(self):
        self.assertAlmostEqual(credential_expires_in(_blob(3600, NOW), now=NOW), 3600.0)
        self.assertAlmostEqual(credential_expires_in(_blob(-10, NOW), now=NOW), -10.0)

    def test_unknown_shapes_are_unknown_not_expired(self):
        for blob in (None, b"", b"not json", b'{"claudeAiOauth": {}}', b'{"claudeAiOauth": {"expiresAt": "soon"}}',
                     b'{"claudeAiOauth": {"expiresAt": true}}'):
            with self.subTest(blob=blob):
                self.assertIsNone(credential_expires_in(blob, now=NOW))


class QuotaReading(unittest.TestCase):
    def test_the_supervisors_markers_apply_to_probe_text(self):
        self.assertIn("resets 3am", quota_reason("You've hit your weekly limit · resets 3am (America/Sao_Paulo)"))
        self.assertIsNone(quota_reason("ok"))


class Decisions(unittest.TestCase):
    def _run(self, *, mode, blob=None, probe_answers=None, probe=False, blob_after=None, env=None):
        calls = {"probe": 0, "resolve": 0}

        def _resolve():
            calls["resolve"] += 1
            if calls["resolve"] > 1 and blob_after is not None:
                return mode, blob_after, "host_login"
            return mode, blob, "host_login" if mode else "no_credentials"

        def _probe(timeout):
            calls["probe"] += 1
            return probe_answers if probe_answers is not None else (True, '{"type":"result","is_error":false,"result":"ok"}')

        import time as _time
        from unittest.mock import patch

        with patch.object(_time, "time", lambda: NOW):
            result = auth_preflight(probe=probe, resolve=_resolve, run_probe=_probe, environ=env or {})
        return result, calls

    def test_no_credential_fails_in_zero_seconds(self):
        result, calls = self._run(mode=None)
        self.assertFalse(result.ok)
        self.assertEqual(calls["probe"], 0)
        self.assertIn("claude login", result.detail)

    def test_a_fresh_subscription_token_is_not_probed(self):
        result, calls = self._run(mode="subscription_login", blob=_blob(5 * 3600, NOW))
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "checked")
        self.assertEqual(calls["probe"], 0)
        self.assertIn("expires in 5.0 h", result.detail)

    def test_an_api_key_needs_nothing(self):
        result, calls = self._run(mode="api_key")
        self.assertTrue(result.ok)
        self.assertEqual(calls["probe"], 0)

    def test_a_token_inside_the_window_is_refreshed_by_a_probe(self):
        result, calls = self._run(
            mode="subscription_login", blob=_blob(10 * 60, NOW), blob_after=_blob(8 * 3600, NOW),
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls["probe"], 1)
        self.assertEqual(result.action, "refreshed")
        self.assertIn("8.0 h", result.detail)
        self.assertLess(10 * 60, AUTH_REFRESH_WINDOW_S)

    def test_an_expired_token_that_the_probe_could_not_refresh_warns_but_proceeds(self):
        result, calls = self._run(
            mode="subscription_login", blob=_blob(-5, NOW), blob_after=_blob(-5, NOW),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "probed")
        self.assertTrue(any("still expires" in w for w in result.warnings))

    def test_a_spent_allowance_stops_the_run_before_the_worker(self):
        result, calls = self._run(
            mode="subscription_login", blob=_blob(5 * 3600, NOW), probe=True,
            probe_answers=(True, "You've hit your weekly limit · resets 3am (America/Sao_Paulo)"),
        )
        self.assertFalse(result.ok)
        self.assertIn("allowance is exhausted", result.detail)
        self.assertIn("resets 3am", result.detail)

    def test_a_dead_login_stops_the_run(self):
        result, _ = self._run(
            mode="subscription_login", blob=_blob(60, NOW),
            probe_answers=(True, "Not logged in · Please run /login"),
        )
        self.assertFalse(result.ok)
        self.assertIn("not logged in", result.detail)

    def test_an_error_envelope_stops_the_run(self):
        result, _ = self._run(
            mode="subscription_login", blob=_blob(60, NOW),
            probe_answers=(True, '{"type":"result","is_error":true,"result":"API Error: 401 OAuth access token has been revoked"}'),
        )
        self.assertFalse(result.ok)

    def test_a_probe_that_could_not_run_is_a_warning_not_a_refusal(self):
        result, _ = self._run(
            mode="subscription_login", blob=_blob(60, NOW),
            probe_answers=(False, "probe timed out after 60s"),
        )
        self.assertTrue(result.ok)
        self.assertTrue(any("did not run" in w for w in result.warnings))

    def test_the_opt_out_skips_everything(self):
        result, calls = self._run(mode=None, env={"SHEPHERD_DEV_NO_AUTH_PREFLIGHT": "1"})
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "skipped")
        self.assertEqual(calls["resolve"], 0)


class CliWiring(unittest.TestCase):
    def test_a_failed_preflight_stops_run_before_the_pack(self):
        import io
        from contextlib import redirect_stderr
        from types import SimpleNamespace
        from unittest.mock import patch

        from shepherd_dev import cli
        from shepherd_dev.preflight import PreflightResult

        args = SimpleNamespace(provider="claude")
        with patch("shepherd_dev.preflight.auth_preflight", return_value=PreflightResult(False, "failed", "no way")):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertFalse(cli._auth_preflight(args, Path("/nonexistent-repo")))
            self.assertIn("auth preflight: no way", err.getvalue())

    def test_other_providers_are_not_checked(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from shepherd_dev import cli

        with patch("shepherd_dev.preflight.auth_preflight") as probe:
            self.assertTrue(cli._auth_preflight(SimpleNamespace(provider="static"), Path("/x")))
            probe.assert_not_called()

    def test_config_turns_the_probe_on_for_every_run(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch

        from shepherd_dev import cli, config
        from shepherd_dev.preflight import PreflightResult

        repo = Path(tempfile.mkdtemp(prefix="shepherd-preflight-"))
        config.save_config(repo, {"preflight": {"auth_probe": True}})
        with patch.object(config, "GLOBAL_CONFIG", repo / "no-global.json"):
            with patch("shepherd_dev.preflight.auth_preflight", return_value=PreflightResult(True, "probed", "fine")) as probe:
                self.assertTrue(cli._auth_preflight(SimpleNamespace(provider="claude"), repo))
                self.assertEqual(probe.call_args.kwargs["probe"], True)


if __name__ == "__main__":
    unittest.main()
