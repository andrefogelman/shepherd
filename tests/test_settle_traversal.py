"""A proposal id names a directory. Unvalidated, it names ANY directory.

`settle_proposal` built its path as `repo_root / ".shepherd-proposals" / id`
with the id taken verbatim from the CLI or the MCP client, and the only check
before `shutil.rmtree` was that the target contained a `files/` subdirectory.

Reproduced before the fix: `settle-par ../tests --reject` deleted the repo's
own tests directory; `../../outside` deleted a directory outside the repo
entirely. On the accept path the same traversal reads a planted manifest.json
and hands its `regate_cmd` to a shell.

The MCP server made this reachable without a human in the loop: accepting
requires confirm=true, but rejecting was documented as "safe" and needed none.

Runnable with: python -m unittest tests.test_settle_traversal
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev.cli import is_proposal_id, settle_proposal  # noqa: E402
from shepherd_dev.staging import PROPOSALS_DIR, stage_proposal  # noqa: E402

#: Every shape that escapes, plus the ones that merely are not ids.
_REJECTED = [
    "../tests",
    "../../outside",
    "..",
    ".",
    "/etc",
    "/",
    "nested/inner",
    "20260727-120000-abcdef/../../..",
    "20260727-120000-ABCDEF",     # the generator emits lowercase hex
    "20260727-120000-abcde",      # six hex digits, not five
    "20260727-120000-abcdefg",    # ...nor seven
    "2026072-120000-abcdef",      # eight digits of date
    "not-a-proposal",
    "",
    ".shepherd-proposals",
    "20260727-120000-abcdef ",    # trailing space
    "․․/tests",         # one-dot-leader lookalike
]


class ProposalIdValidation(unittest.TestCase):
    def test_a_generated_id_is_accepted(self):
        repo = Path(mkdtemp())
        pid, _ = stage_proposal(repo, {"a.py": b"A\n"}, {"feature": "f"})
        self.assertTrue(is_proposal_id(pid), pid)

    def test_every_escaping_or_malformed_shape_is_refused(self):
        for candidate in _REJECTED:
            with self.subTest(candidate=candidate):
                self.assertFalse(is_proposal_id(candidate))


class SettleRefusesTraversal(unittest.TestCase):
    def setUp(self):
        self.root = Path(mkdtemp())
        self.repo = self.root / "repo"
        (self.repo / PROPOSALS_DIR).mkdir(parents=True)
        # a directory inside the repo that is not a proposal, shaped so the
        # old `files/` existence check would have waved it through
        (self.repo / "tests" / "files").mkdir(parents=True)
        (self.repo / "tests" / "test_real.py").write_text("# real work\n")
        # ...and one outside it
        (self.root / "outside" / "files").mkdir(parents=True)
        (self.root / "outside" / "backup.txt").write_text("data\n")

    def test_reject_does_not_delete_a_directory_inside_the_repo(self):
        code, written = settle_proposal(self.repo, "../tests", reject=True)
        self.assertEqual(code, 2)
        self.assertEqual(written, [])
        self.assertTrue((self.repo / "tests" / "test_real.py").is_file())

    def test_reject_does_not_delete_a_directory_outside_the_repo(self):
        code, _ = settle_proposal(self.repo, "../../outside", reject=True)
        self.assertEqual(code, 2)
        self.assertTrue((self.root / "outside" / "backup.txt").is_file())

    def test_accept_does_not_read_a_planted_manifest(self):
        """The accept path feeds manifest.json's regate_cmd to a shell, so the
        traversal is not only a delete primitive."""
        planted = self.repo / "tests"
        (planted / "manifest.json").write_text(
            '{"regate_cmd": "touch %s"}' % (self.root / "PWNED")
        )
        code, _ = settle_proposal(self.repo, "../tests", reject=False)
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "PWNED").exists())

    def test_a_real_proposal_still_settles(self):
        pid, _ = stage_proposal(self.repo, {"src/new.py": b"N = 1\n"}, {"feature": "f"})
        code, written = settle_proposal(self.repo, pid, reject=False)
        self.assertEqual(code, 0)
        self.assertEqual(written, ["src/new.py"])
        self.assertTrue((self.repo / "src" / "new.py").is_file())

    def test_a_real_proposal_still_rejects(self):
        pid, _ = stage_proposal(self.repo, {"src/new.py": b"N = 1\n"}, {"feature": "f"})
        code, _ = settle_proposal(self.repo, pid, reject=True)
        self.assertEqual(code, 0)
        self.assertFalse((self.repo / PROPOSALS_DIR / pid).exists())


if __name__ == "__main__":
    unittest.main()
