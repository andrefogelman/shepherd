"""Codex executor: binary resolution, argv construction, fake factory."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.providers.codex_exec import (  # noqa: E402
    CliCodexExecutor,
    FakeCodexExecutor,
    build_executor,
    find_codex_bin,
)


class FindCodexBin(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(find_codex_bin("/x/codex"), "/x/codex")

    def test_env_wins_over_path(self):
        with mock.patch.dict(os.environ, {"SHEPHERD_DEV_CODEX_CMD": "/env/codex"}):
            self.assertEqual(find_codex_bin(), "/env/codex")

    def test_path_lookup(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHEPHERD_DEV_CODEX_CMD", None)
            with mock.patch("shutil.which", return_value="/usr/bin/codex"):
                self.assertEqual(find_codex_bin(), "/usr/bin/codex")


class ArgvConstruction(unittest.TestCase):
    def test_headless_exec_argv(self):
        ex = CliCodexExecutor(codex_bin="/bin/codex", model="gpt-5.5")
        clone = Path("/tmp/clone")
        argv = ex.build_argv(clone, "do the thing")
        self.assertEqual(argv[0], "/bin/codex")
        self.assertEqual(argv[1], "exec")
        self.assertIn("-C", argv)
        self.assertEqual(argv[argv.index("-C") + 1], str(clone))
        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertIn("--skip-git-repo-check", argv)
        self.assertIn("--ephemeral", argv)
        self.assertIn("-m", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.5")
        self.assertEqual(argv[-1], "do the thing")

    def test_no_model_flag_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHEPHERD_DEV_CODEX_MODEL", None)
            ex = CliCodexExecutor(codex_bin="/bin/codex")
            argv = ex.build_argv(Path("/tmp/c"), "p")
        self.assertNotIn("-m", argv)

    def test_sandbox_env_override(self):
        with mock.patch.dict(
            os.environ, {"SHEPHERD_DEV_CODEX_SANDBOX": "danger-full-access"}
        ):
            ex = CliCodexExecutor(codex_bin="/bin/codex")
            argv = ex.build_argv(Path("/tmp/c"), "p")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "danger-full-access")


class Factory(unittest.TestCase):
    def test_fake_files_wins(self):
        ex = build_executor(fake_files={"a.txt": b"x"})
        self.assertIsInstance(ex, FakeCodexExecutor)

    def test_env_fake(self):
        with mock.patch.dict(os.environ, {"SHEPHERD_DEV_CODEX_FAKE": "1"}):
            ex = build_executor()
        self.assertIsInstance(ex, FakeCodexExecutor)

    def test_real_cli_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHEPHERD_DEV_CODEX_FAKE", None)
            ex = build_executor(codex_bin="/bin/codex")
        self.assertIsInstance(ex, CliCodexExecutor)


class FakeExecutor(unittest.TestCase):
    def test_writes_files(self):
        clone = Path(tempfile.mkdtemp())
        res = FakeCodexExecutor({"pkg/mod.py": b"ok\n"}).run(clone, "x", budget_seconds=5)
        self.assertTrue(res.ok)
        self.assertEqual((clone / "pkg" / "mod.py").read_bytes(), b"ok\n")

    def test_blocks_escape(self):
        clone = Path(tempfile.mkdtemp())
        res = FakeCodexExecutor({"../evil.py": b"x"}).run(clone, "x", budget_seconds=5)
        self.assertFalse(res.ok)

    def test_fail_mode(self):
        res = FakeCodexExecutor(fail=True, error="boom").run(
            Path(tempfile.mkdtemp()), "x", budget_seconds=5
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "boom")


if __name__ == "__main__":
    unittest.main()
