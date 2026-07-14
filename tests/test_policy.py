"""Tests for the changeset policy hardening (#2 nested-forbidden + traversal) and
the remote overlay path guard (#1 tar-slip defense-in-depth). Runnable with:
    python -m unittest tests.test_policy
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.policy import ChangesetPolicy, check_paths  # noqa: E402
from shepherd_dev.remotegate import _is_safe_rel, _overlay, parse_remote_config  # noqa: E402


def _bad(paths):
    return check_paths(paths, ChangesetPolicy()).violations


class ForbiddenNested(unittest.TestCase):
    def test_nested_forbidden_dirs_caught(self):
        for p in ("src/.env", "config/.env.local", "pkg/node_modules/x",
                  "nested/.git/hooks/pre-commit", "a/.venv/lib/y", "deep/.shepherd-proposals/z"):
            self.assertTrue(_bad([p]), f"{p} should be forbidden")

    def test_top_level_forbidden_still_caught(self):
        for p in (".env", ".git/config", "node_modules/pkg/index.js"):
            self.assertTrue(_bad([p]), f"{p} should be forbidden")

    def test_clean_path_ok(self):
        self.assertEqual(check_paths(["src/app.py", "lib/util.ts"], ChangesetPolicy()).violations, [])


class Traversal(unittest.TestCase):
    def test_escapes_are_rejected(self):
        for p in ("../escape", "src/../../etc/passwd", "/etc/passwd", "~/.ssh/authorized_keys", "\\\\host\\share"):
            v = _bad([p])
            self.assertTrue(any("escapes the repo" in x for x in v), f"{p} should escape")


class AllowedPrefix(unittest.TestCase):
    def test_outside_prefix_flagged(self):
        pol = ChangesetPolicy(allowed_prefixes=("src/",))
        self.assertTrue(check_paths(["lib/x.py"], pol).violations)
        self.assertEqual(check_paths(["src/x.py"], pol).violations, [])


class RemoteOverlayGuard(unittest.TestCase):
    def test_is_safe_rel(self):
        self.assertTrue(_is_safe_rel("src/a.py"))
        for bad in ("../x", "a/../../b", "/abs", "~/x", "\\x"):
            self.assertFalse(_is_safe_rel(bad), bad)

    def test_overlay_refuses_unsafe_before_any_ssh(self):
        cfg = parse_remote_config({"ssh": "root@host", "repo_dir": "/x", "test_cmd": "true"}, None)
        assert cfg is not None
        # unsafe entry -> refusal string, returned WITHOUT touching ssh/subprocess
        err = _overlay(cfg, "/tmp/wd", {"../evil": b"x"}, timeout=5)
        assert err is not None
        self.assertIn("unsafe path", err)


class SettleProposalSymlink(unittest.TestCase):
    def test_symlink_in_stage_is_skipped(self):  # #18
        import os
        import tempfile

        from shepherd_dev.cli import settle_proposal

        root = Path(tempfile.mkdtemp())
        secret = Path(tempfile.mkdtemp()) / "secret.txt"
        secret.write_text("TOPSECRET")
        files = root / ".shepherd-proposals" / "p1" / "files"
        files.mkdir(parents=True)
        (files / "real.py").write_text("x = 1\n")
        os.symlink(secret, files / "leak.txt")  # a symlink pointing OUTSIDE the repo

        code, written = settle_proposal(root, "p1", reject=False)
        self.assertEqual(code, 0)
        self.assertIn("real.py", written)
        self.assertNotIn("leak.txt", written)           # symlink skipped
        self.assertFalse((root / "leak.txt").exists())   # external secret NOT copied in


if __name__ == "__main__":
    unittest.main()
