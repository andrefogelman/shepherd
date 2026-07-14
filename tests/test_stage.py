"""Test #5: staged proposal ids don't collide within the same second (uuid suffix).
Runnable with: python -m unittest tests.test_stage
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.parallel import _stage_proposal  # noqa: E402


class ProposalIdCollision(unittest.TestCase):
    def test_two_stages_get_distinct_ids(self):
        root = Path(tempfile.mkdtemp())
        id1, w1 = _stage_proposal(root, {"a.py": b"1\n"}, {"feature": "x"})
        id2, w2 = _stage_proposal(root, {"b.py": b"2\n"}, {"feature": "y"})
        self.assertNotEqual(id1, id2)  # even back-to-back (same second), the uuid suffix differs
        self.assertTrue((root / ".shepherd-proposals" / id1 / "files").is_dir())
        self.assertTrue((root / ".shepherd-proposals" / id2 / "files").is_dir())


if __name__ == "__main__":
    unittest.main()
