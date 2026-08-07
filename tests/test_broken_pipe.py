"""A truncated stdout must announce itself, not pass for the whole story.

Seed: a caller ran `shepherd-dev run … | head`, read the first lines, and
treated them as the run's outcome. `head` exits after its count, the pipe
closes, and every later write raises BrokenPipeError — which CPython reports,
if at all, as "Exception ignored" noise at interpreter shutdown. The run's
real record was never missing: --json, --review-report, the event journal and
`shepherd-dev trace <id>` all survive a closed stdout, and the trace hint
already prints on stderr. It was simply not noticed.

So the fix is not another surface. It is shepherd noticing that its own
output was cut and saying so where the cut cannot reach: stderr.

Runnable with: python -m unittest tests.test_broken_pipe
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SIGPIPE_EXIT = 141  # 128 + SIGPIPE(13), what a shell reports for this


class BrokenPipeIsAnnouncedTests(unittest.TestCase):
    def _main_with(self, handler, *, run_id=None):
        """Drive main() with a fake command handler, capturing stderr."""
        import io

        import shepherd_dev.cli as C

        class _Args:
            command = "status"

            def func(self, *a):
                return handler()

        args = _Args()
        args.func = lambda *a: handler()

        real_parser = C.build_parser
        real_stderr = sys.stderr
        real_last = C.last_run_id()
        C.set_last_run_id(run_id)
        C.build_parser = lambda: type("P", (), {"parse_args": lambda self, *a: args})()
        sys.stderr = io.StringIO()
        try:
            code = C.main()
            return code, sys.stderr.getvalue()
        finally:
            sys.stderr = real_stderr
            C.build_parser = real_parser
            C.set_last_run_id(real_last)

    def test_a_clean_command_is_untouched(self):
        code, err = self._main_with(lambda: 0)
        self.assertEqual(code, 0)
        self.assertNotIn("truncated", err)

    def test_a_broken_pipe_becomes_a_message_on_stderr(self):
        def _boom():
            raise BrokenPipeError(32, "Broken pipe")

        code, err = self._main_with(_boom)
        self.assertEqual(code, SIGPIPE_EXIT)
        self.assertIn("truncated", err.lower())

    def test_the_message_names_the_run_so_it_can_be_recovered(self):
        def _boom():
            raise BrokenPipeError(32, "Broken pipe")

        _, err = self._main_with(_boom, run_id="20260807-101010-abcdef")
        self.assertIn("shepherd-dev trace 20260807-101010-abcdef", err)

    def test_without_a_run_id_it_still_points_somewhere_useful(self):
        def _boom():
            raise BrokenPipeError(32, "Broken pipe")

        _, err = self._main_with(_boom, run_id=None)
        self.assertIn("shepherd-dev status", err)

    def test_an_ordinary_exception_is_not_swallowed_as_a_pipe_break(self):
        def _boom():
            raise RuntimeError("a real failure")

        with self.assertRaises(RuntimeError):
            self._main_with(_boom)


class RunIdIsRecordedTests(unittest.TestCase):
    def test_the_accessor_round_trips(self):
        import shepherd_dev.cli as C

        before = C.last_run_id()
        try:
            C.set_last_run_id("20260807-000000-aaaaaa")
            self.assertEqual(C.last_run_id(), "20260807-000000-aaaaaa")
        finally:
            C.set_last_run_id(before)


class RealPipeTests(unittest.TestCase):
    """The unit tests above fake the exception. This one makes a real `head`
    close a real pipe, which is the only way to know the handler is reached
    at all — CPython's own shutdown path competes for this."""

    #: A command that prints far more than `head -1` will take, driven through
    #: the real main() in a real interpreter, so the real pipe closes and the
    #: real shutdown flush happens — the two things a faked exception cannot
    #: reproduce.
    DRIVER = """
import sys
sys.path.insert(0, {src!r})
import shepherd_dev.cli as C

class _Args:
    command = "status"
    def func(self, *a):
        for i in range(50000):
            print("line", i)
        return 0

args = _Args()
args.func = lambda *a: _Args.func(args)
C.build_parser = lambda: type("P", (), {{"parse_args": lambda self, *a: args}})()
C.set_last_run_id("20260807-121212-fedcba")
sys.exit(C.main())
"""

    def test_a_real_head_truncation_is_reported_on_stderr(self):
        src = str(Path(__file__).resolve().parent.parent / "src")
        driver = Path(__file__).resolve().parent / "_bp_driver_tmp.py"
        driver.write_text(self.DRIVER.format(src=src), encoding="utf-8")
        self.addCleanup(lambda: driver.unlink(missing_ok=True))

        proc = subprocess.run(
            ["bash", "-c", f"{sys.executable} -u {driver} | head -1"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertIn("truncated", proc.stderr.lower(), proc.stderr)
        self.assertIn("shepherd-dev trace 20260807-121212-fedcba", proc.stderr)
        self.assertNotIn(
            "Exception ignored", proc.stderr,
            "CPython's shutdown noise must not be what the caller sees instead",
        )


if __name__ == "__main__":
    unittest.main()
