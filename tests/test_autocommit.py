"""Test #8: auto_commit_branch must never leave the repo on the shepherd branch —
it commits on an isolated branch and always returns to the original, even when a
step fails midway. Runnable with: python -m unittest tests.test_autocommit
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev.cli import auto_commit_branch  # noqa: E402


def _git(root, *argv):
    return subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True)


def _repo() -> Path:
    root = Path(mkdtemp())
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.co")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


def _branch(root) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


class AutoCommitBranch(unittest.TestCase):
    def test_commits_and_returns_to_original(self):
        root = _repo()
        original = _branch(root)
        (root / "feature.py").write_text("x = 1\n")  # an accepted file in the worktree
        branch, err = auto_commit_branch(root, ["feature.py"], "add-feature", "feat: x")
        self.assertIsNone(err)
        self.assertTrue(branch and branch.startswith("shepherd/"))
        assert branch is not None
        # back on the original branch, and the shepherd branch exists with the commit
        self.assertEqual(_branch(root), original)
        self.assertEqual(_git(root, "rev-parse", "--verify", "--quiet", branch).returncode, 0)

    def test_accepted_files_stay_in_the_worktree_after_a_successful_commit(self):
        """The finally's comment and settle's own message both promise the
        accepted files remain in the working tree. On the SUCCESS path they did
        not: committing them onto shepherd/* and then checking out the original
        branch deletes them from the worktree, because that is where they now
        exclusively live."""
        root = _repo()
        (root / "feature.py").write_text("x = 1\n")            # new file
        (root / "seed.txt").write_text("seed\nchanged\n")      # modified tracked file
        branch, err = auto_commit_branch(
            root, ["feature.py", "seed.txt"], "add-feature", "feat: x"
        )
        self.assertIsNone(err)
        self.assertTrue((root / "feature.py").is_file())
        self.assertEqual((root / "feature.py").read_text(), "x = 1\n")
        self.assertEqual((root / "seed.txt").read_text(), "seed\nchanged\n")

    def test_restored_files_are_not_left_staged(self):
        root = _repo()
        (root / "feature.py").write_text("x = 1\n")
        auto_commit_branch(root, ["feature.py"], "add-feature", "feat: x")
        staged = _git(root, "diff", "--cached", "--name-only").stdout.strip()
        self.assertEqual(staged, "", "restoring must not leave an index full of changes")

    def test_files_stay_in_the_worktree_when_a_step_fails(self):
        root = _repo()
        (root / "feature.py").write_text("x = 1\n")
        # 'missing.py' makes `git add` fail, so nothing is committed
        auto_commit_branch(root, ["feature.py", "missing.py"], "bad", "feat: bad")
        self.assertTrue((root / "feature.py").is_file())

    def test_returns_to_original_even_when_a_step_fails(self):
        root = _repo()
        original = _branch(root)
        # 'written' names a path that does not exist -> `git add` fails mid-way;
        # the finally must still restore the original branch (not strand us on shepherd/*).
        branch, err = auto_commit_branch(root, ["does-not-exist.py"], "bad", "feat: bad")
        self.assertIsNotNone(err)
        self.assertEqual(_branch(root), original)


if __name__ == "__main__":
    unittest.main()
