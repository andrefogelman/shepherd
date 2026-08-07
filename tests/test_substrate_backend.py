"""`shepherd init` must survive a carrier the platform cannot tear down.

Seed: three CI runs failed on ubuntu with the same five tests, each one
funnelling through `shepherd init`:

    error: shepherd init failed: Error: workspace initialized but activation
    failed: Failed to unmount .../ground/merged: fusermount3: failed to
    unmount ...: Operation not permitted

The substrate's probe (vcs_core.substrates.detect_overlay_backend) checks
that /dev/fuse, fuse-overlayfs and fusermount3 EXIST. On a GitHub runner
they exist and mounting works — unmounting does not. So `auto` resolves to
a carrier that cannot complete its own lifecycle, and the workspace is
initialized but declared failed. macOS never saw it: there `auto` resolves
to clonefile.

Runnable with: python -m unittest tests.test_substrate_backend
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

UNMOUNT_ERR = (
    "Error: workspace initialized but activation failed: Failed to unmount "
    "/tmp/vcs-core-overlay/x/ground/merged: fusermount3: failed to unmount "
    "/tmp/vcs-core-overlay/x/ground/merged: Operation not permitted"
)


class _Proc:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


class BackendSelectionTests(unittest.TestCase):
    def setUp(self):
        import os

        self._env = os.environ.pop("SHEPHERD_DEV_BACKEND", None)
        self.addCleanup(
            lambda: os.environ.__setitem__("SHEPHERD_DEV_BACKEND", self._env)
            if self._env is not None
            else None
        )

    def test_no_backend_flag_by_default(self):
        from shepherd_dev.cli import shepherd_init_argv

        self.assertEqual(shepherd_init_argv("/bin/shepherd"), ["/bin/shepherd", "init"])

    def test_an_explicit_backend_is_passed_through(self):
        import os

        from shepherd_dev.cli import shepherd_init_argv

        os.environ["SHEPHERD_DEV_BACKEND"] = "copy"
        self.assertEqual(
            shepherd_init_argv("/bin/shepherd"),
            ["/bin/shepherd", "init", "--backend", "copy"],
        )

    def test_a_bogus_backend_is_ignored_rather_than_passed_on(self):
        """argparse on the other side would reject it and the message would
        blame us; an unknown value means the env is wrong, not the run."""
        import os

        from shepherd_dev.cli import shepherd_init_argv

        os.environ["SHEPHERD_DEV_BACKEND"] = "banana"
        self.assertEqual(shepherd_init_argv("/bin/shepherd"), ["/bin/shepherd", "init"])


class UnmountFailureFallsBackTests(unittest.TestCase):
    def _run_init(self, results):
        """Drive run_shepherd_init with a scripted sequence of subprocess results."""
        import shepherd_dev.cli as C

        calls = []
        seq = list(results)

        def _fake_run(argv, **kw):
            calls.append(list(argv))
            return seq.pop(0)

        real = C.subprocess.run
        C.subprocess.run = _fake_run
        try:
            err = C.run_shepherd_init(Path("/bin/shepherd"), Path("/repo"))
        finally:
            C.subprocess.run = real
        return err, calls

    def test_a_clean_init_runs_once_and_reports_nothing(self):
        err, calls = self._run_init([_Proc(0)])
        self.assertIsNone(err)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--backend", calls[0])

    def test_an_unmount_failure_retries_on_the_portable_carrier(self):
        err, calls = self._run_init([_Proc(1, UNMOUNT_ERR), _Proc(0)])
        self.assertIsNone(err, "the retry succeeded, so the init succeeded")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][-2:], ["--backend", "copy"])

    def test_a_failure_that_is_not_the_carrier_is_reported_without_a_retry(self):
        """Retrying a real error would hide it behind a second failure."""
        err, calls = self._run_init([_Proc(1, "fatal: not a git repository")])
        self.assertIsNotNone(err)
        self.assertIn("not a git repository", err)
        self.assertEqual(len(calls), 1, "no retry for an unrelated failure")

    def test_when_the_retry_also_fails_the_original_error_survives(self):
        err, calls = self._run_init([_Proc(1, UNMOUNT_ERR), _Proc(1, "copy carrier broke")])
        self.assertIsNotNone(err)
        self.assertIn("copy carrier broke", err)
        self.assertEqual(len(calls), 2)

    def test_an_explicit_backend_is_not_second_guessed(self):
        """If the caller named a carrier, a failure is theirs to see."""
        import os

        os.environ["SHEPHERD_DEV_BACKEND"] = "fuse"
        self.addCleanup(lambda: os.environ.pop("SHEPHERD_DEV_BACKEND", None))
        err, calls = self._run_init([_Proc(1, UNMOUNT_ERR)])
        self.assertIsNotNone(err)
        self.assertEqual(len(calls), 1)

    def test_the_error_text_is_taken_from_stdout_when_stderr_is_empty(self):
        err, _ = self._run_init([_Proc(1, "", "boom on stdout")])
        self.assertIn("boom on stdout", err)


class CarrierErrorDetectionTests(unittest.TestCase):
    def test_the_seed_message_is_recognised(self):
        from shepherd_dev.cli import _is_carrier_lifecycle_error

        self.assertTrue(_is_carrier_lifecycle_error(UNMOUNT_ERR))

    def test_ordinary_failures_are_not(self):
        from shepherd_dev.cli import _is_carrier_lifecycle_error

        for text in ("fatal: not a git repository", "permission denied: /repo", ""):
            with self.subTest(text=text):
                self.assertFalse(_is_carrier_lifecycle_error(text))


if __name__ == "__main__":
    unittest.main()
