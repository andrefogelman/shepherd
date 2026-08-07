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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev.contextpack import (  # noqa: E402
    build_pack,
    repo_file_view,
    scan_repo,
)


def _repo(files: dict[str, str]) -> Path:
    root = Path(mkdtemp())
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


class SharedRepoScan(unittest.TestCase):
    """A2: the repo walk + file reads do not depend on the feature, but runN
    redid them for every one of its N features (twice each, counting the
    planning prefetch's own view). One scan, reused."""

    def _sample(self):
        return _repo({
            "svc/orders.py": "from svc import util\n\ndef list_orders():\n    return []\n",
            "svc/util.py": "def helper():\n    return 1\n",
            "svc/payments.py": "def charge():\n    return True\n",
            "tests/test_orders.py": "from svc.orders import list_orders\n\ndef test_x():\n    pass\n",
        })

    def test_pack_is_byte_identical_with_and_without_a_shared_scan(self):
        root = self._sample()
        scan = scan_repo(root)
        for feature in ("orders listing", "payments charge"):
            with self.subTest(feature=feature):
                plain, plain_stats = build_pack(root, feature)
                shared, shared_stats = build_pack(root, feature, scan=scan)
                self.assertEqual(plain, shared)
                self.assertEqual(plain_stats, shared_stats)

    def test_repo_file_view_matches_too(self):
        root = self._sample()
        scan = scan_repo(root)
        self.assertEqual(repo_file_view(root), repo_file_view(root, scan=scan))

    def test_a_shared_scan_walks_the_repo_once_for_n_features(self):
        from shepherd_dev import contextpack as CP

        root = self._sample()
        features = ["orders listing", "payments charge", "util helper"]

        real = CP._iter_files
        walks = {"n": 0}

        def counting(repo_root, allowed_prefixes):
            walks["n"] += 1
            return real(repo_root, allowed_prefixes)

        CP._iter_files = counting
        try:
            for f in features:
                build_pack(root, f)
                repo_file_view(root)
            per_feature = walks["n"]

            walks["n"] = 0
            scan = scan_repo(root)
            for f in features:
                build_pack(root, f, scan=scan)
                repo_file_view(root, scan=scan)
            shared = walks["n"]
        finally:
            CP._iter_files = real

        self.assertEqual(per_feature, 2 * len(features))  # pack + planning view
        self.assertEqual(shared, 1)

    def test_allowed_prefixes_are_honoured_by_the_scan(self):
        root = self._sample()
        scan = scan_repo(root, allowed_prefixes=("svc",))
        pack, _ = build_pack(root, "orders listing", allowed_prefixes=("svc",), scan=scan)
        self.assertNotIn("tests/test_orders.py", pack)

    def test_a_scan_taken_for_other_prefixes_is_refused(self):
        # Silently reusing a narrower scan would quietly change the pack.
        root = self._sample()
        scan = scan_repo(root, allowed_prefixes=("svc",))
        with self.assertRaises(ValueError):
            build_pack(root, "orders listing", allowed_prefixes=(), scan=scan)


class ElixirBuildDirsAreNotContextTests(unittest.TestCase):
    """Measured on a real Phoenix repo: of the 4000 files the scan is capped
    at, 3969 were under deps/ and 27 belonged to the repo. The ignore list
    covered node_modules, vendor and target — Node, PHP, Rust — and nothing
    for Elixir, so every Elixir pack was third-party dependency source.

    Alphabetical order is what makes it total rather than partial: rglob is
    sorted, so `_build/` and `deps/` are consumed before `lib/` is reached.
    """

    def _repo(self, dep_files: int) -> Path:
        from tmpdirs import mkdtemp

        root = Path(mkdtemp(prefix="shepherd-pack-elixir-"))
        (root / "lib" / "app_web").mkdir(parents=True)
        (root / "lib" / "app_web" / "router.ex").write_text(
            'defmodule AppWeb.Router do\n  scope "/" do\n  end\nend\n'
        )
        for name, sub in (("_build", "dev/lib/app/ebin"), ("deps", "phoenix/lib")):
            d = root / name / sub
            d.mkdir(parents=True)
            for i in range(dep_files):
                (d / f"mod_{i}.ex").write_text(f"defmodule Vendor.M{i} do\nend\n")
        return root

    def test_both_dirs_are_classified_as_build_output(self):
        from shepherd_dev.contextpack import PACK_IGNORED_DIRS

        self.assertIn("deps", PACK_IGNORED_DIRS)
        self.assertIn("_build", PACK_IGNORED_DIRS)

    def test_the_scan_returns_the_repos_own_files_not_its_dependencies(self):
        root = self._repo(dep_files=5)
        scanned = {str(p.relative_to(root)) for p in scan_repo(root).files}
        self.assertIn("lib/app_web/router.ex", scanned)
        self.assertFalse(
            [rel for rel in scanned if rel.startswith(("deps/", "_build/"))],
            "dependency and build-output sources are not this repo's context",
        )

    def test_a_large_dependency_tree_no_longer_crowds_out_the_repo(self):
        """The seed failure in miniature: with the scan cap lowered to the
        size of the dependency tree, the repo's own file survived only
        because deps/ is skipped before the cap is ever reached."""
        import shepherd_dev.contextpack as CP

        root = self._repo(dep_files=40)
        real_cap = CP.SCAN_FILE_CAP
        CP.SCAN_FILE_CAP = 10
        try:
            scanned = {str(p.relative_to(root)) for p in scan_repo(root).files}
        finally:
            CP.SCAN_FILE_CAP = real_cap
        self.assertIn("lib/app_web/router.ex", scanned)

    def test_the_pack_built_from_such_a_repo_talks_about_the_repo(self):
        root = self._repo(dep_files=5)
        pack, stats = build_pack(root, "add a route to the router")
        self.assertIn("router.ex", pack)
        self.assertNotIn("Vendor.M0", pack)


if __name__ == "__main__":
    unittest.main()
