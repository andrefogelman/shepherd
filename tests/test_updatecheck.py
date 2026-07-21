"""Tests for the update notice: cache-based (never blocks a command), the
background refresh writes the cache for the NEXT invocation, semver compare,
opt-out, and total failure tolerance.
Runnable with: python -m unittest tests.test_updatecheck
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev import updatecheck as U  # noqa: E402


class SemverCompareTests(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(U._is_newer("0.1.23", than="0.1.22"))
        self.assertTrue(U._is_newer("0.2.0", than="0.1.99"))
        self.assertTrue(U._is_newer("1.0.0", than="0.9.9"))

    def test_not_newer(self):
        self.assertFalse(U._is_newer("0.1.22", than="0.1.22"))
        self.assertFalse(U._is_newer("0.1.21", than="0.1.22"))

    def test_garbage_is_never_newer(self):
        self.assertFalse(U._is_newer("", than="0.1.22"))
        self.assertFalse(U._is_newer("abc", than="0.1.22"))
        self.assertFalse(U._is_newer("0.1.23", than="garbage"))


class CacheAndNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-upd-")
        self.addCleanup(self.tmp.cleanup)
        self._old = U.CACHE_FILE
        U.CACHE_FILE = Path(self.tmp.name) / "update-check.json"
        self.addCleanup(setattr, U, "CACHE_FILE", self._old)

    def _write_cache(self, latest: str, ts: float | None = None):
        U.CACHE_FILE.write_text(json.dumps({"latest": latest, "ts": ts or time.time()}))

    def test_notice_when_cache_has_newer(self):
        self._write_cache("9.9.9")
        notice = U.update_notice(current="0.1.22")
        self.assertIsNotNone(notice)
        self.assertIn("9.9.9", notice)
        self.assertIn("0.1.22", notice)
        self.assertIn("shepherd-dev update", notice)

    def test_no_notice_when_cache_same_or_older(self):
        self._write_cache("0.1.22")
        self.assertIsNone(U.update_notice(current="0.1.22"))
        self._write_cache("0.1.1")
        self.assertIsNone(U.update_notice(current="0.1.22"))

    def test_no_notice_without_cache_or_with_bad_cache(self):
        self.assertIsNone(U.update_notice(current="0.1.22"))
        U.CACHE_FILE.write_text("not json{")
        self.assertIsNone(U.update_notice(current="0.1.22"))

    def test_opt_out_env(self):
        import os

        self._write_cache("9.9.9")
        os.environ["SHEPHERD_DEV_NO_UPDATE_CHECK"] = "1"
        self.addCleanup(os.environ.pop, "SHEPHERD_DEV_NO_UPDATE_CHECK", None)
        self.assertIsNone(U.update_notice(current="0.1.22"))

    def test_refresh_writes_cache_from_remote_pyproject(self):
        def fake_fetch(url, timeout):
            self.assertIn("pyproject.toml", url)
            return 'name = "shepherd-dev"\nversion = "3.2.1"\n'

        U._refresh_cache(fetch=fake_fetch)
        cached = json.loads(U.CACHE_FILE.read_text())
        self.assertEqual(cached["latest"], "3.2.1")
        self.assertAlmostEqual(cached["ts"], time.time(), delta=5)

    def test_refresh_swallows_network_errors(self):
        def boom(url, timeout):
            raise OSError("no network")

        U._refresh_cache(fetch=boom)  # must not raise
        self.assertFalse(U.CACHE_FILE.exists())

    def test_fresh_cache_skips_refresh(self):
        self._write_cache("0.1.22")
        calls = []

        def counting_fetch(url, timeout):
            calls.append(url)
            return 'version = "0.1.22"\n'

        U.maybe_refresh_in_background(fetch=counting_fetch)
        time.sleep(0.2)
        self.assertEqual(calls, [])  # TTL not expired → no fetch

    def test_stale_cache_triggers_background_refresh(self):
        self._write_cache("0.1.22", ts=time.time() - U.TTL_SECONDS - 10)
        done = []

        def fetch(url, timeout):
            done.append(1)
            return 'version = "5.0.0"\n'

        thread = U.maybe_refresh_in_background(fetch=fetch)
        self.assertIsNotNone(thread)
        thread.join(3)
        self.assertEqual(done, [1])
        self.assertEqual(json.loads(U.CACHE_FILE.read_text())["latest"], "5.0.0")


class FetchLatestTests(unittest.TestCase):
    def test_returns_version(self):
        got = U.fetch_latest(fetch=lambda url, timeout: 'version = "2.0.0"\n')
        self.assertEqual(got, "2.0.0")

    def test_none_on_failure_or_garbage(self):
        def boom(url, timeout):
            raise OSError("offline")

        self.assertIsNone(U.fetch_latest(fetch=boom))
        self.assertIsNone(U.fetch_latest(fetch=lambda u, t: "no version here"))


class CmdUpdateTests(unittest.TestCase):
    """`shepherd-dev update` — explicit, synchronous, human-invoked upgrade."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shepherd-cmdupd-")
        self.addCleanup(self.tmp.cleanup)
        self._old = U.CACHE_FILE
        U.CACHE_FILE = Path(self.tmp.name) / "update-check.json"
        self.addCleanup(setattr, U, "CACHE_FILE", self._old)

    def _run(self, *, latest, current="0.1.24", force=False, which="/usr/local/bin/uv", rc=0):
        import contextlib
        import io

        from shepherd_dev import updatecheck as UU

        calls: list[list[str]] = []

        def fake_fetch(url, timeout):
            if latest is None:
                raise OSError("offline")
            return f'version = "{latest}"\n'

        def fake_run(argv, **kw):
            calls.append(list(argv))

            class _P:
                returncode = rc

            return _P()

        old_run, old_which = UU.subprocess.run, UU.shutil.which
        UU.subprocess.run = fake_run
        UU.shutil.which = lambda name: which if name == "uv" else old_which(name)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = UU.run_update(current=current, force=force, fetch=fake_fetch)
        finally:
            UU.subprocess.run, UU.shutil.which = old_run, old_which
        return code, calls, buf.getvalue()

    def test_upgrades_when_newer(self):
        code, calls, out = self._run(latest="9.9.9")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("uv", calls[0][0])
        self.assertIn("--force", calls[0])
        self.assertIn("9.9.9", out)

    def test_already_latest_no_install(self):
        code, calls, out = self._run(latest="0.1.24")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn("already", out)

    def test_force_reinstalls_same_version(self):
        code, calls, _ = self._run(latest="0.1.24", force=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_offline_fails_loud(self):
        code, calls, out = self._run(latest=None)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("could not", out)

    def test_missing_uv_fails_with_manual_hint(self):
        code, calls, out = self._run(latest="9.9.9", which=None)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("uv", out)

    def test_installer_failure_propagates(self):
        code, calls, _ = self._run(latest="9.9.9", rc=2)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)

    def test_success_refreshes_the_cache(self):
        self._run(latest="9.9.9")
        cached = json.loads(U.CACHE_FILE.read_text())
        self.assertEqual(cached["latest"], "9.9.9")


if __name__ == "__main__":
    unittest.main()
