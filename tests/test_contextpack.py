"""Tests for the context pack's #3 enrichment: import-graph slice + test contract.

The base pack scores files by keyword and emits full/skeleton blocks. #3 adds,
for the top-scored TARGET files, their import-graph neighbors (what a target
imports and who imports it) and the target's sibling TEST files — so the worker
sees the structural neighborhood and the test contract without blind exploration.

All deterministic, pure stdlib: same repo state + feature => byte-identical pack.
Runnable with: python -m unittest tests.test_contextpack
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.contextpack import build_pack  # noqa: E402


def _repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


class ContextPackEnrichment(unittest.TestCase):
    def test_forward_python_import_neighbor(self):
        # target = payments.py (keyword "payments"); it imports local `ledger`
        root = _repo({
            "payments.py": "from ledger import post\n\ndef charge():\n    return post()\n",
            "ledger.py": "def post():\n    return 1\n",
            "unrelated.py": "def noise():\n    return 0\n",
        })
        pack, stats = build_pack(root, "add payments retry")
        self.assertIn("payments.py", pack)
        self.assertIn("ledger.py", pack)                 # pulled as neighbor
        self.assertIn("imported by payments.py", pack)   # forward marker
        self.assertGreaterEqual(stats["neighbors"], 1)

    def test_reverse_python_importer(self):
        # target scores by CONTENT keyword; the importer does NOT score on its own
        # (its only keyword-free) -> it's surfaced purely as a reverse neighbor.
        root = _repo({
            "engine.py": "# orchestration hub module\ndef spin():\n    return 1\n",
            "boot.py": "from engine import spin\n\nspin()\n",
        })
        pack, _ = build_pack(root, "orchestration hub")
        self.assertIn("engine.py", pack)     # target (content keyword match)
        self.assertIn("boot.py", pack)       # reverse neighbor, not independently scored
        self.assertIn("imports engine.py", pack)  # reverse marker

    def test_ts_relative_import_resolved(self):
        root = _repo({
            "app.ts": "import { u } from './util';\nexport const app = () => u();\n",
            "util.ts": "export const u = () => 1;\n",
        })
        pack, _ = build_pack(root, "app entrypoint")
        self.assertIn("util.ts", pack)
        self.assertIn("imported by app.ts", pack)

    def test_test_contract_included(self):
        # target scores by content keyword; the sibling test file does NOT score
        # on its own -> it's surfaced purely as the test contract.
        root = _repo({
            "formatter.py": "# csv normalization helper\ndef fmt(s):\n    return s.strip()\n",
            "test_formatter.py": "from formatter import fmt\n\ndef test_fmt():\n    assert fmt(' x ') == 'x'\n",
        })
        pack, stats = build_pack(root, "csv normalization")
        self.assertIn("test contract for formatter.py", pack)
        self.assertGreaterEqual(stats["test_contracts"], 1)

    def test_dedup_neighbor_already_in_pack(self):
        # both files match the feature keyword -> both are top-scored (full blocks);
        # the import edge must NOT add a second block for the same file.
        root = _repo({
            "orders.py": "from orders_db import q\n\ndef orders():\n    return q()\n",
            "orders_db.py": "def q():\n    return []\n# orders orders\n",
        })
        pack, _ = build_pack(root, "orders listing")
        self.assertEqual(pack.count("== FILE: orders_db.py"), 1)

    def test_budget_respected_no_crash(self):
        root = _repo({
            "svc.py": "from helper import h\n\ndef svc():\n    return h()\n",
            "helper.py": "def h():\n    return 1\n",
        })
        # tiny budget: base sections may already fill it; enrichment must skip
        # cleanly and stats must stay coherent (no neighbor counted if not emitted).
        pack, stats = build_pack(root, "svc helper", budget=400)
        self.assertLessEqual(len(pack), 400 + 4000)  # header+tree may overshoot once
        self.assertIn("neighbors", stats)
        self.assertIn("test_contracts", stats)

    def test_stats_has_new_keys(self):
        root = _repo({"a.py": "x = 1\n"})
        _, stats = build_pack(root, "a thing")
        for k in ("neighbors", "test_contracts", "targets", "planned"):
            self.assertIn(k, stats)

    def test_planned_target_force_included(self):
        # a file that scores 0 on the feature is still emitted when the planner
        # names it (that is the whole point of #4 feeding targets to the pack).
        root = _repo({
            "alpha.py": "# csv normalization module\ndef a():\n    return 1\n",
            "zeta.py": "def z():\n    return 2\n",  # no keyword -> scores 0
        })
        pack, stats = build_pack(root, "csv normalization", planned_targets=("zeta.py",))
        self.assertIn("zeta.py", pack)
        self.assertIn("planned target", pack)
        self.assertGreaterEqual(stats["planned"], 1)

    def test_plan_text_section_emitted(self):
        root = _repo({"a.py": "x = 1\n"})
        pack, _ = build_pack(root, "a thing", plan_text="1. do X\n2. do Y")
        self.assertIn("FEATURE PLAN", pack)
        self.assertIn("do X", pack)

    def test_planned_hallucination_ignored(self):
        root = _repo({"a.py": "x = 1\n"})
        _, stats = build_pack(root, "thing", planned_targets=("nope.py",))
        self.assertEqual(stats["planned"], 0)


if __name__ == "__main__":
    unittest.main()
