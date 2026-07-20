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
        self.assertIn("uv tool install", notice)

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


if __name__ == "__main__":
    unittest.main()
