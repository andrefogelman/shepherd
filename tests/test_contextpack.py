"""Tests for the context pack: the #3 enrichment (import-graph slice + test
contract) and the cut that keeps capped sections closed (`cut_to_fit`,
`workspace_instructions`).

The base pack scores files by keyword and emits full/skeleton blocks. #3 adds,
for the top-scored TARGET files, their import-graph neighbors (what a target
imports and who imports it) and the target's sibling TEST files — so the worker
sees the structural neighborhood and the test contract without blind exploration.
`CutToFit` and `WorkspaceInstructionsBudget` pin the paragraph cut and the
budget shared across the instruction files.

All deterministic, pure stdlib: same repo state + feature => byte-identical pack.
Runnable with: python -m unittest tests.test_contextpack
"""

from __future__ import annotations

import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev import contextpack  # noqa: E402
from shepherd_dev.contextpack import (  # noqa: E402
    _MARKER_RESERVE,
    _open_span,
    _raw_cut,
    _unclosed_fence,
    _walk_fences,
    PLAN_TEXT_CAP,
    TRUNCATION_MARKER,
    build_pack,
    cut_to_fit,
    repo_file_view,
    scan_repo,
    workspace_instructions,
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
        # Every section header, the neighbour's included, is followed by a blank line.
        self.assertEqual(re.findall(r"^== [^\n]*==\n(?!\n)", pack, re.M), [])

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

    def test_a_planned_files_skeleton_does_not_leave_a_block_open(self):
        # Over FULL_FILE_LIMIT a Markdown file is shown by its first lines; a fence open at
        # the end of that slice swallowed the next file section of the pack.
        for opener, closer, pad in (("```python", "```", ""), ("~~~~", "~~~~", ""),
                                    ("````", "````", ""), ("- x\n\n  ```", "  ```", "  ")):
            with self.subTest(opener=opener):
                guide = "# Guide\n\n" + "text\n" * 20 + opener + "\n" + (pad + "x = 1\n") * 800
                root = _repo({"guide.md": guide, "b.py": "def b():\n    return 2\n"})
                pack, _ = build_pack(root, "a thing", planned_targets=("guide.md", "b.py"))
                section = pack.split("== FILE: guide.md", 1)[1].split("\n", 1)[1]
                section = section.split("== FILE:", 1)[0]
                self.assertIsNone(_unclosed_fence(section), section[-80:])
                self.assertTrue(section.rstrip("\n").endswith("\n" + closer), section[-40:])
                self.assertIn("== FILE: b.py", pack)
                self.assertEqual(re.findall(r"^== [^\n]*==\n(?!\n)", pack, re.M), [])

    def test_a_whole_file_that_ends_inside_a_block_does_not_swallow_the_next(self):
        # Files under FULL_FILE_LIMIT go in whole: one that ends inside a fence (an unfinished
        # Markdown file, an unpaired fence in a docstring or a comment) swallowed every
        # section after it. Each file block starts after a blank line, too.
        for name, body in (("widget.md", "# feat\n\n```python\nwidget code\n"),
                           ("widget.md", "widget\n\n```"),
                           ("widget.py", 'def widget():\n    """\n```\n"""\n'),
                           ("widget.ts", "/* widget\n~~~\n*/\nexport const w = 1\n")):
            with self.subTest(name=name, body=body):
                root = _repo({name: body, "zz_tail.py": "def tail():\n    return 2\n"})
                pack, _ = build_pack(root, "add widget", planned_targets=(name,))
                section = pack.split(f"== FILE: {name}", 1)[1].split("\n", 1)[1]
                section = section.split("== FILE:", 1)[0]
                self.assertTrue(section.startswith("\n"), section[:20])
                self.assertIsNone(_unclosed_fence(section), section[-60:])
                self.assertIsNone(_unclosed_fence(pack.split("== FILE: zz_tail.py", 1)[0]))
                self.assertEqual(re.findall(r"^== [^\n]*==\n(?!\n)", pack, re.M), [])
        # The closer sits on a line of its own, with no blank line before it.
        root = _repo({"a.md": "# feat\n\n```python\nwidget code\n", "b.md": "widget\n\n```"})
        pack, _ = build_pack(root, "widget feat", planned_targets=("a.md", "b.md"))
        self.assertIn("widget code\n```\n", pack)
        self.assertIn("widget\n\n```\n```\n", pack)

    def test_skeleton_blocks_get_a_blank_line_after_their_header(self):
        big = "def widget_%d():\n    return %d\n"
        tests_text = "".join("def test_%d():\n    pass\n" % i for i in range(600))
        root = _repo({"widget.py": "".join(big % (i, i) for i in range(400)),
                      "src/core.py": "def core():\n    return 1\n",
                      "tests/test_core.py": tests_text})
        pack, _ = build_pack(root, "widget", planned_targets=("src/core.py",))
        self.assertIn("widget.py (signatures only", pack)
        self.assertIn("test_core.py (test contract for", pack)
        self.assertEqual(re.findall(r"^== [^\n]*==\n(?!\n)", pack, re.M), [])

    def test_a_file_name_with_a_newline_stays_on_its_header_line(self):
        # A name holding a newline would have put "```x.py (full) ==" on a line of its own:
        # a fence opener that swallowed every later section. It is printed escaped.
        root = _repo({"widget\n```x.py": "def widget():\n    return 1\n",
                      "widget_b.py": "def b():\n    return 2\n"})
        pack, _ = build_pack(root, "widget")
        self.assertIn("== FILE: widget\\n```x.py (full) ==", pack)
        self.assertIn("widget\\n```x.py\n", pack.split("== REPO FILE TREE ==", 1)[1])
        for block in pack.split("== FILE: ")[1:]:
            self.assertIsNone(_unclosed_fence(block), block[:80])

    def test_every_path_the_pack_prints_stays_on_one_line(self):
        # One odd name per site: planned target (full and signatures), keyword file
        # (signatures), neighbour and its label, test contract and its label. Printable
        # non-ASCII names are left as they are.
        odd = "w\n```"
        big = "".join("def f_%d():\n    return %d\n" % (i, i) for i in range(400))
        root = _repo({
            f"{odd}a.ts": f'import x from "./{odd}c"\n',  # planned target; imports a neighbour
            f"{odd}c.ts": "export const x = 1\n",
            f"{odd}big.py": big,  # planned, over the limit: signatures
            f"{odd}d.py": "def d():\n    return 1\n",  # planned target with a test contract
            f"tests/test_{odd}d.py": "def test_d():\n    pass\n",
            f"{odd}e.ts": f'import y from "./{odd}a"\n',  # imports the planned target
            f"{odd}g.py": "def g():\n    return 1\n",  # planned, with a large test contract
            f"tests/test_{odd}g.py": big.replace("def f_", "def test_"),
            f"{odd}kw_cafe.py": big,  # keyword-scored, over the limit
            "café_w.py": "def w():\n    return 1\n",
        })
        planned = (f"{odd}a.ts", f"{odd}big.py", f"{odd}d.py", f"{odd}g.py")
        pack, _ = build_pack(root, "cafe café", planned_targets=planned)
        for line in pack.split("\n"):
            if line.startswith("== FILE: "):
                self.assertTrue(line.endswith("=="), line)
        for shown in ("w\\n```a.ts (planned target; full)", "w\\n```big.py (planned target; sig",
                      "w\\n```c.ts (imported by w\\n```a.ts", "w\\n```kw_cafe.py (signatures",
                      "(test contract for w\\n```d.py", "w\\n```e.ts (imports w\\n```a.ts",
                      "test_w\\n```g.py (test contract for w\\n```g.py; signatures", "café_w.py"):
            self.assertIn(shown, pack)
        self.assertNotIn("caf\\xe9", pack)
        for block in pack.split("== FILE: ")[1:]:
            self.assertIsNone(_unclosed_fence(block), block[:80])

    def test_a_quote_fence_in_an_item_is_read_from_the_items_column(self):
        flags = [ln.taken for ln in _walk_fences("- > ```\n    > a ` b\n")]
        self.assertEqual(flags, [True, True, False])
        text = "- > ```\n    > code\n    > ```\n  > prose `a span that runs on and on` end\n"
        self.assertIn("  > prose ", cut_to_fit(text, 60))

    def test_a_lone_cr_in_a_file_is_a_line_ending_to_the_closer(self):
        # CommonMark ends a line at a lone CR: "```" after one opens a block there.
        root = _repo({"widget.md": "x\r```\rbody", "zz.py": "def z():\n    return 2\n"})
        pack, _ = build_pack(root, "widget", planned_targets=("widget.md",))
        self.assertIn("x\n```\nbody\n```\n", pack)
        # A CRLF ending is left as it is: no blank line where there was none.
        root = _repo({"widget.md": "x\r\n```\r\nbody", "zz.py": "def z():\n    return 2\n"})
        pack, _ = build_pack(root, "widget", planned_targets=("widget.md",))
        self.assertIn("x\r\n```\r\nbody\n```\n", pack)

    def test_a_path_that_looks_like_a_fence_does_not_swallow_the_pack(self):
        root = _repo({"```/widget.py": "def widget():\n    return 1\n",
                      "b.py": "def b():\n    return 2\n"})
        pack, _ = build_pack(root, "widget")
        tree = pack.split("== REPO FILE TREE ==\n", 1)[1].split("== FILE:", 1)[0]
        self.assertIsNone(_unclosed_fence(tree), tree)
        self.assertIn("== FILE: ", pack)

    def test_whole_files_from_keywords_and_test_contracts_are_closed_too(self):
        # The keyword-scored path (no planned target).
        root = _repo({"widget.md": "# widget\n\n```python\nwidget code\n",
                      "zz_tail.py": "def tail():\n    return 2\n"})
        pack, _ = build_pack(root, "widget")
        self.assertIn("== FILE: widget.md (full)", pack)
        self.assertIsNone(_unclosed_fence(pack.split("== FILE: zz_tail.py", 1)[0]), pack[-200:])
        # The test-contract path: a target's test file whose docstring leaves a fence open.
        root = _repo({"src/widget.py": "def widget():\n    return 1\n",
                      "tests/test_widget.py": "def test_w():\n    \"\"\"\n```\n\"\"\"\n",
                      "zz_tail.py": "def tail():\n    return 2\n"})
        pack, _ = build_pack(root, "a thing", planned_targets=("src/widget.py",))
        self.assertIn("test contract", pack)
        contract = pack.split("tests/test_widget.py (test contract", 1)[1].split("\n", 1)[1]
        self.assertIsNone(_unclosed_fence(contract.split("== FILE:", 1)[0]), contract[:120])
        self.assertEqual(re.findall(r"^== [^\n]*==\n(?!\n)", pack, re.M), [])

    def test_plan_text_section_emitted(self):
        root = _repo({"a.py": "x = 1\n"})
        pack, _ = build_pack(root, "a thing", plan_text="1. do X\n2. do Y")
        self.assertIn("FEATURE PLAN", pack)
        # A blank line after the header: the plan is a container of its own.
        self.assertIn("== FEATURE PLAN (pre-computed; follow it) ==\n\n", pack)
        self.assertIn("do X", pack)

    def test_plan_text_over_the_cap_is_cut_and_marked(self):
        root = _repo({"a.py": "x = 1\n"})
        pack, _ = build_pack(root, "a thing", plan_text="x" * (PLAN_TEXT_CAP + 500))
        self.assertIn(TRUNCATION_MARKER, pack)
        kept = pack.split("FEATURE PLAN", 1)[1].split("\n\n", 1)[1].split(TRUNCATION_MARKER, 1)[0]
        # The cap covers the whole section: the plan that survived, its
        # newline and the marker.
        self.assertLessEqual(len(kept) + len(TRUNCATION_MARKER), PLAN_TEXT_CAP)

    def test_plan_text_cut_backs_out_of_an_unclosed_fence(self):
        root = _repo({"a.py": "x = 1\n"})
        # the blank line before the block sits in the second half
        prose = "p " * (PLAN_TEXT_CAP * 3 // 10)
        plan = "1. do A\n\n" + prose + "\n\n```sh\nmake all\n\n" + "x" * PLAN_TEXT_CAP + "\n```\n"
        self.assertGreater(plan.index("```sh"), (PLAN_TEXT_CAP - _MARKER_RESERVE) // 2)
        pack, _ = build_pack(root, "a thing", plan_text=plan)
        kept = pack.split("FEATURE PLAN", 1)[1].split(TRUNCATION_MARKER, 1)[0]
        self.assertNotIn("```", kept)  # backed out to the prose, no closer appended

    def test_plan_text_raw_fallback_closes_the_block(self):
        root = _repo({"a.py": "x = 1\n"})
        plan = "```sh\nmake all\n" + "x" * (PLAN_TEXT_CAP + 100) + "\n```\n"  # no blank line at all
        pack, _ = build_pack(root, "a thing", plan_text=plan)
        kept = pack.split("FEATURE PLAN", 1)[1].split(TRUNCATION_MARKER, 1)[0]
        self.assertEqual(kept.count("```"), 2, kept[-80:])

    def test_plan_text_with_lone_cr_line_endings_is_folded_before_the_cut(self):
        # The planner's JSON may carry CR endings; the walk splits on LF only.
        root = _repo({"a.py": "x = 1\n"})
        plan = "intro\r\r```\rcode\r" + "x" * (PLAN_TEXT_CAP + 100) + "\r```\rafter\r"
        pack, _ = build_pack(root, "a thing", plan_text=plan)
        kept = pack.split("FEATURE PLAN", 1)[1].split(TRUNCATION_MARKER, 1)[0]
        self.assertNotIn("\r", kept)
        self.assertEqual(kept.count("```"), 2, kept[-80:])

    def test_plan_text_with_crlf_endings_keeps_its_lines_together(self):
        root = _repo({"a.py": "x = 1\n"})
        pack, _ = build_pack(root, "a thing", plan_text="1. a\r\n   b\r\n2. c")
        self.assertIn("==\n\n1. a\n   b\n2. c\n", pack)

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


#: Inline-span cases: text and the offset of the run left open ("last" for the
#: last backtick, None for none); shared with the fuzz oracle's cross-check.
SPAN_CASES = (
    ("a\n~~~\n```\n`c`\n~~~\nd `e`", None),
    ("a\n~~~\n```\n`c`\n~~~\nd `e", "last"),  # the last, unmatched backtick
    ("a\r\nb `c", "last"),  # a CR is one character like any other
    ("```foo```", None),  # the span line counts as prose, and closes
    ("run ``git `status` then", 4),  # a shorter run inside is literal
    ("``a`` b", None),
    ("stray ` tick\n\nnext paragraph", None),  # a paragraph ends a span
    ("stray ` tick\n \nnext paragraph", None),  # a whitespace-only line too
    ("a ` b\n```\nx\n```\nc", None),  # and so does a fenced block
    ("a ` b\n```\nx\n```\nc `d", "last"),
    ("```\na ` b", None),  # a backtick inside an open block is content
    ("```foo` bar\nmore", 0),  # a fence-looking span line is prose: its run stays open
    # an OPENER ends the paragraph too, even with the block still open
    ("a ` b\n```\nx", None),
    ("a `\n# head\nb c", None),  # an ATX heading ends the paragraph
    ("# head `\nb c", None),  # and the heading itself is one line
    ("a `\n---\nb c", None),  # a thematic break (or setext underline) too
    ("a `\n- item\nb c", None),  # a list item too
    ("a `\n- item `x\nb c", "last"),  # and the item's own span then opens
    ("a `\n1. item\nb c", None),
    ("a `\n1) item\nb c", None),  # with either delimiter
    ("a `\n001. item\nb c", None),  # an ordinal is read by value
    ("a `\n0000000001. item\nb c", 2),  # ten digits: not a list marker, the span stays open
    ("x `\n===\ny `z", "last"),  # a setext underline ends the paragraph above
    ("x `\n--\ny `z", "last"),
    ("note `foo\n> quote\nuse `bar", "last"),  # the first quote line too
    ("> use `git\n> fetch` ok", None),  # consecutive quote lines are one paragraph
    ("` x\n-*-\n` y `", "last"),  # not a break: three of ONE character are needed
    ("` a\n2. b\n` c `", "last"),  # an ordered list interrupts only from 1
    ("` a\n+\n` c `", "last"),  # an empty item does not interrupt
    ("a `\n#tag\nb `c", None),  # no space after #: not a heading
    ("a `\n####### h\nb `c", None),  # seven #: not a heading (spec ex. 63)
    ("a `\n-item\nb `c", None),  # no space after -: not an item
    ("> `b\n>\n> `c` d `e", "last"),  # a blank quote line ends the quote's paragraph
    ("> a\n> # h `\n> b `c", "last"),  # a heading inside the quote is one line
    ("> a `\n> - b `c", "last"),  # an item inside the quote starts a block
    ("> a `\n> > b `c", "last"),  # and so does a quote inside the quote
    # a 2. that ends the bullet item starts a list, and a paragraph
    ("- a `\n2. b `c", "last"),
    ("- a `\nb `c", None),  # a lazy line continues the item's paragraph
    ("- a `\n    # h `\n  b `c", "last"),  # a heading inside the item, read from its column
    ("- a `\n    # h\n  b `c", "last"),
    ("- > a `\n  > b `c", None),  # a quote inside the item: its lines are one paragraph
    ("- > q `\nb `c", None),  # and a lazy line continues it, through the quote
    # and the marked line after a lazy one continues it too
    ("> a `\nb\n> c `d `e", "last"),
    ("- > a `\nb\n  > c `d `e", "last"),
    ("> bar\nbaz\n> foo `x", "last"),  # the spec's own laziness example
    # the quote's paragraph runs through the lazy line and on
    ("> a\nlazy `a\n> x `", None),
    ("> > a `\nlazy\n> b `c", None),  # through a nested quote's paragraph as well
    ("> > > a `\nlazy\n> b `c", None),
    ("> > a `\n> > b `c", None),  # nested quote lines are one paragraph too
    ("> > a `\n> b `c", None),  # fewer markers continue it (laziness)
    ("> > a `\n> b\n> > c `d", None),
    ("a `\n\u00a0\nb `c", None),  # an NBSP line is paragraph text, not a blank line
    ("a `\n- \u00a0b `c", "last"),  # an item whose content starts with an NBSP interrupts
    ("- a\n\t# h `", None),  # a tab-indented heading inside the item is one line
    ("- a `\n\t# h\n  b `c", "last"),
    ("a `\n#\nb `c", "last"),  # a bare # is a heading: it ends the paragraph, and `c opens
    ("a `\n**\nb `c", None),  # two of a character are not a break
    ("a `\n- \x0cb `c", "last"),  # a form feed is content: the item interrupts
    ("> ```", None),  # a quote's fence line is not a span
    ("> ```\n> a ` b", None),  # nor is its code
    ("- > ```\n    > a ` b", None),  # a quote fence in an item, its lines four columns in
    ("- a `\n===\n  b `c", None),  # a lazy === line is the item's paragraph text
    ("- a `\nlazy\n===\n  b `c", None),  # also after a lazy line: an underline is never lazy
    # a line that ends one paragraph and opens another moves the column a === is judged at
    ("1. a\n- `c\n   ===\n   d` e", "last"),
    # a paragraph opened after a blank line, or under an empty marker, lives at its own column
    ("-\n  c `\n===", "last"),
    ("- a\n  - b\n\n  c `\n  ===\n  d `e", "last"),
    ("- * * *\n    ```\n    x\n  ```\n  a `b", "last"),  # the item's break, block and prose
    ("> a `\n===\nb `c", None),  # and so it is under a quote
    ("> a\n===\nb `c\n> d `", None),  # a bare === under the quote's lazy paragraph is lazy text too
    ("Say \\` opens `x y` and more", None),  # a backslash escapes the backtick after it
    ("Say \\` opens `x y", "last"),
    ("`a \\` b", None),  # inside a span a backslash escapes nothing: the run closes it
    ("\\\\`a` b", None),  # two backslashes: the backtick is live
    ("\\``a", 2),  # an escaped backtick heading a longer run: the live one opens
    ("a `\n    > b `c", None),  # four spaces before >: a lazy line, not a quote
    ("a `\r\n---\r\nb `c", "last"),  # a CRLF break ends the paragraph
    # one space after > is the marker's: a heading follows
    ("> a `\n>    # h\n> b `c", "last"),
    ("> a `\n>     # h\n> b `c", None),  # a fifth space: continuation text, not a heading
    # but a heading left no paragraph: lazy is new, and > x starts a quote
    ("> # h\nlazy `a\n> x `", "last"),
    ("> ---\nlazy `a\n> x `", "last"),
    ("> ===\nb `c\n> d `", None),  # > === leaves a paragraph open, as prose: b is lazy, > d goes on
    ("> ===\nlazy `a\n> x `", None),
    # the item after the lazy line starts a paragraph of its own
    ("> a `\nb\n2. c `d", "last"),
    # and closes the quote: the next > line opens a new one
    ("> a `\nb\n- c `d\n> e `", "last"),
    ("> a `\nb\n2. c `d\n> e `", "last"),
    ("> a\nb `c\n2. d `e", "last"),
    ("a `\n    # h\nb `c", None),  # an indented heading is prose: the paragraph goes on
)


class CutToFit(unittest.TestCase):
    """The pack's cut lands on a blank line and never leaves a code block or an
    inline span open.

    Cutting at a raw character offset put the marker inside whatever the
    offset hit: a bash fence in the Gate section, then an inline span. The
    worker then read an open block that swallowed the marker and the sections
    the pack appended after it. A blank line closes any inline span, and a
    fence tracker keeps the cut out of a block; the raw fallback closes what
    it leaves open.
    """

    # A fence with a blank line inside it: the last blank line before the
    # limit is in the block, so the cut has to back out of it.
    FIXTURE = (
        "# Rules\n\nFirst paragraph of prose that the worker needs.\n\n"
        "## Gate\n\n```bash\ngit fetch --prune origin\n\n"
        "git branch -r --no-merged origin/main\ngh pr list\n```\n\nAfter the fence.\n"
    )

    def test_text_that_fits_is_untouched(self):
        self.assertEqual(cut_to_fit("short", 100), "short")
        self.assertEqual(cut_to_fit("x" * 100, 100), "x" * 100)

    def test_cut_is_the_last_blank_line_that_fits(self):
        text = "\n\n".join(f"paragraph {i} " + "word " * 10 for i in range(20))
        out = cut_to_fit(text, 300)
        self.assertLessEqual(len(out), 300)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertTrue(body.endswith("\n\n"), repr(body[-20:]))
        keep = 300 - _MARKER_RESERVE
        self.assertEqual(body[:-2], text[: text.rfind("\n\n", 0, keep)])
        nxt = text[len(body):].split("\n\n", 1)[0]  # the next paragraph did not fit
        self.assertGreater(len(body) + len(nxt), keep)

    def test_a_blank_line_at_the_floor_is_used_and_one_before_it_is_not(self):
        limit = 60
        keep = limit - _MARKER_RESERVE
        floor = keep // 2
        at_floor = "a" * floor + "\n\n" + "b" * 200
        self.assertEqual(cut_to_fit(at_floor, limit), "a" * floor + "\n\n" + TRUNCATION_MARKER)
        below = "a" * (floor - 1) + "\n\n" + "b" * 200
        out = cut_to_fit(below, limit)
        self.assertEqual(len(out), limit)
        self.assertIn("b", out.split(TRUNCATION_MARKER)[0])

    def test_a_blank_line_ending_exactly_at_keep_is_used(self):
        keep = 60 - _MARKER_RESERVE
        text = "a" * (keep - 2) + "\n\n" + "b" * 200
        self.assertEqual(cut_to_fit(text, 60), "a" * (keep - 2) + "\n\n" + TRUNCATION_MARKER)
        straddling = "a" * (keep - 1) + "\n\n" + "b" * 200  # the blank line does not fit: raw path
        out = cut_to_fit(straddling, 60)
        self.assertEqual(len(out), 60)
        # raw prefix, then the join
        self.assertEqual(out.split(TRUNCATION_MARKER)[0], "a" * (keep - 1) + "\n\n")

    def test_an_empty_raw_prefix_yields_the_marker_alone(self):
        # Room for three characters: the opener's run alone, which its closer cannot follow.
        self.assertEqual(cut_to_fit("```\n" + "x" * 100, _MARKER_RESERVE + 3), TRUNCATION_MARKER)
        # With room for one character the window still sees the whole run: nothing is kept.
        out = cut_to_fit("```\ncode\n" + "x" * 100, _MARKER_RESERVE + 1)
        self.assertEqual(out, TRUNCATION_MARKER)
        self.assertEqual(_raw_cut("```\ncode", 1), "")
        # The window stops keep past the cut: a backtick beyond it that would make a span line
        # of the stub is not seen, so the stub is backed out (conservative, never an open block).
        self.assertEqual(cut_to_fit("```" + "x" * 100 + "`y", 60), TRUNCATION_MARKER)
        # Inside an open block `- ```x` is content, not an opener: the cut keeps its stub.
        self.assertEqual(_raw_cut("```\nab\n- ```x" + "y" * 50, 13), "```\nab\n- \n```")
        self.assertEqual(_raw_cut("   ```\nx", 1), "")  # the floor sees a three-space opener too
        self.assertEqual(_raw_cut("\n- - ~~~", 5), "")  # and the window reaches keep past the cut

    def test_a_lone_backtick_at_the_edge_is_literal_and_kept(self):
        out = cut_to_fit("`" + "x" * 100, _MARKER_RESERVE + 1)
        self.assertEqual(out, "`\n" + TRUNCATION_MARKER)

    def test_backs_out_of_a_fence_the_limit_falls_inside(self):
        keep = 150 - _MARKER_RESERVE
        self.assertIn("```bash", self.FIXTURE[:keep])  # the block opened before the limit
        self.assertEqual(self.FIXTURE[:keep].count("```"), 1)  # and did not close
        out = cut_to_fit(self.FIXTURE, 150)
        self.assertLessEqual(len(out), 150)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertTrue(body.endswith("## Gate\n\n"), repr(body[-30:]))

    def test_backs_out_of_a_fence_with_blank_lines_inside(self):
        # The blank lines inside the block are not a way out: the cut keeps
        # backing up until the block is closed, here to the intro.
        intro = "Intro paragraph. " * 4
        block = "```bash\nstep one\n\nstep two\n\nstep three\n\nstep four\n```"
        text = intro + "\n\n" + block + "\n\nAfter.\n"
        self.assertGreater(len(text), 110)  # the limit lands inside the block
        out = cut_to_fit(text, 110)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertEqual(body, intro + "\n\n", repr(body))

    def test_one_long_paragraph_falls_back_to_the_raw_cut(self):
        out = cut_to_fit("R" * 50_000, 4_000)
        self.assertEqual(len(out), 4_000)
        self.assertTrue(out.endswith("\n" + TRUNCATION_MARKER))

    def test_raw_fallback_closes_an_indented_block_at_its_own_indentation(self):
        # A column-0 closer would end the list item and open a new block.
        text = "- item\n  ```sh\n  make all\n  " + "x" * 200
        out = cut_to_fit(text, 40 + _MARKER_RESERVE)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertTrue(body.rstrip().endswith("\n  ```"), repr(body[-20:]))
        self.assertIsNone(_unclosed_fence(body))

    def test_a_block_on_the_items_next_line_gets_no_closer_after_a_dedent(self):
        out = cut_to_fit("- item\n  ```sh\n  make all\nyy\n" + "y" * 300, 60)
        body = out.split(TRUNCATION_MARKER)[0]
        # nothing opened after the dedent
        self.assertNotIn("  ```", body.split("yy", 1)[1], repr(body))
        self.assertIsNone(_unclosed_fence(body))

    def test_walker_flags_fence_looking_content_inside_a_block(self):
        flags = [ln.taken for ln in _walk_fences("```\n    ```\n````x\n- ```\nprose")]
        self.assertEqual(flags, [True, True, True, False, False])

    def test_a_cut_inside_a_fence_looking_content_line_backs_up(self):
        # Inside a ``` block the line ````x is content; a cut after its four
        # backticks would leave a prefix whose last line reads as a closer.
        out = _raw_cut("```\nab\n````x\n" + "y" * 50, 11)
        self.assertIsNone(_unclosed_fence(out))
        self.assertEqual(out, "```\nab\n```")

    def test_a_marker_line_inside_an_open_block_is_content(self):
        out = cut_to_fit("```sh\n- ```\n" + "x" * 500, 60)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertIsNone(_unclosed_fence(body))
        self.assertTrue(body.rstrip().endswith("\n```"), repr(body[-20:]))

    def test_a_dedented_line_after_an_item_block_gets_no_closer(self):
        self.assertEqual(_raw_cut("- ```sh\n  x\nyy\n" + "y" * 50, 20), "- ```sh\n  x\nyy\nyyyyy")

    def test_a_block_opened_on_a_list_marker_line_is_closed_and_left_behind(self):
        text = "- ```sh\n  make all\n  ```\n\nmore prose\n\n" + "y" * 300
        out = cut_to_fit(text, 60)
        self.assertEqual(out, "- ```sh\n  make all\n  ```\n\nmore prose\n\n" + TRUNCATION_MARKER)

    def test_raw_fallback_does_not_pair_a_span_across_a_heading(self):
        # The stray backtick before the heading is literal; the one after it
        # opens a span (closed further on) the cut must back out of.
        text = "use ` here\n# Title\nthen `git fetch` " + "z" * 50
        out = _raw_cut(text, 30)
        self.assertEqual(out, "use ` here\n# Title\nthen ")

    def test_raw_fallback_closes_an_open_block_with_its_own_fence(self):
        for opener in ("```", "````", "~~~"):
            with self.subTest(opener=opener):
                out = cut_to_fit(opener + "bash\n" + "x" * 5_000, 200)
                self.assertLessEqual(len(out), 200)
                body = out.split(TRUNCATION_MARKER)[0]
                self.assertTrue(body.rstrip().endswith("\n" + opener), repr(body[-30:]))
                self.assertIsNone(_unclosed_fence(body))

    def test_raw_fallback_when_the_opener_sits_at_the_edge(self):
        # The trim that makes room for the closer can remove the opener
        # itself; a closer appended then would OPEN a block.
        keep = 60 - _MARKER_RESERVE
        text = "x" * (keep - 4) + "\n```\n" + "y" * 500
        self.assertEqual(_unclosed_fence(text[:keep]), "```")  # the branch is reached
        out = cut_to_fit(text, 60)
        self.assertLessEqual(len(out), 60)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertNotIn("```", body, repr(out))  # opener trimmed away, no closer added
        self.assertIsNone(_unclosed_fence(body))

    def test_raw_fallback_trim_through_a_partial_opener_leaves_no_open_span(self):
        keep = 60 - _MARKER_RESERVE
        text = "x" * (keep - 6) + "\n```bash\n" + "y" * 500  # head ends "\n```ba"
        out = cut_to_fit(text, 60)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertIsNone(_unclosed_fence(body))
        self.assertIsNone(_open_span(body), repr(body[-12:]))

    def test_fence_tracker_edges(self):
        for text, want in (
            ("  ```py\nx\n  ```", None),
            ("   ~~~\nx", "   ~~~"),  # indentation travels with the run
            ("    ```\nx", None),  # four spaces: indented code, not a fence
            ("``\nx", None),  # two backticks: not a fence
            ("~~\nx", None),
            ("```\nx\n````", None),  # a closer longer than the opener closes
            ("````\n```\nx\n```", "````"),  # a shorter run inside a longer block is content
            ("~~~\n```\nx\n```", "~~~"),  # backtick lines never close a tilde block
            ("```\n```bash\nx", "```"),  # a run with an info string never closes
            ("```\nx\n```  ", None),  # trailing spaces after a closer are fine
            ("```\r\nx\r\n```\r", None),  # and so is a CR
            ("```bash  \nx", "```"),  # trailing spaces after an info string
            ("```foo```\nx", None),  # a backtick fence cannot carry a backtick: inline span
            ("~~~foo`bar\nx", "~~~"),  # a tilde fence's info string may hold one
            ("```\nx\n  ```", None),  # indentation of 0-3 is free on either side
            ("  ```\nx\n```", None),
            ("   ```\nx\n ```", None),
            ("- a\n  - b\n    ```sh\n    x", "    ```"),  # nested items: columns add up
            # a block on the item's next line lives in the item too
            ("- item\n  ```sh\n  x\nyy", None),
            # a column-0 run under it ends the item and OPENS a block
            ("- item\n  ```sh\n  x\n```", "```"),
            ("# h\n2. ```\n   x", "   ```"),  # only prose refuses an item numbered other than 1
            ("- a\n- ```\n  x", "  ```"),  # a sibling item's marker line can open a block
            # known deviation: an HTML block is not tracked, its run opens a fence
            ("<div>\n```\nx", "```"),
            ("- ```sh\n  x\n  ```\nmore", None),  # a block may open on the item's first line
            ("1. ```\n   x\n   ```\nmore", None),
            ("- ```sh\n  x", "  ```"),  # and closes at the marker's content column
            # a closer may sit up to 3 past the column
            ("1.   ```\n     x\n     ```\n     more", None),
            ("1.   ```\n     x\n        ```\n     more", None),
            ("1.   ```\n     x\n         ```\n     more", "     ```"),  # four past it is content
            # a closer below the item's column ends the item and OPENS a block
            ("- ```sh\n  x\n```", "```"),
            ("1.   ```\n     x\n   ```", "   ```"),
            # a dedented line ends the item and its block, no closer needed
            ("- ```sh\n  x\nyy", None),
            ("```\n- ```\nx", "```"),  # inside a block a marker line is content, never a closer
            ("```\n1. ```\nx", "```"),
            ("~~~\n  - ~~~\nx", "~~~"),
            ("a\n2. ```\n   x", None),  # after prose, an item numbered other than 1 is text
            ("a\n1. ```\n   x", "   ```"),  # numbered 1, it is an item, and the block opens
            ("a\n1) ```\n   x", "   ```"),  # with either delimiter
            ("a\n####### h\n2. ```\n   x", None),  # seven #: paragraph text, so 2. is text too
            ("a\n01. ```\n    x", "    ```"),  # 01 is 1 as well
            ("- a\n01. ```\n    x", "    ```"),
            # after a blank line it is an item, and the block opens
            ("a\n\n2. ```\n   x", "   ```"),
            # an indented heading is prose, so the paragraph goes on
            ("a\n    # h\n2. ```\n   x", None),
            ("a\n    - b\n2. ```\n   x", None),
            ("1. a\n2. ```\n   x", "   ```"),  # but the list's own next item is an item
            ("- a\n  1. b\n  2. ```\n     x", "     ```"),
            ("1. a\n   - b\n2. ```\n   x", "   ```"),  # the sibling ends the nested list too
            # a 2. under a bullet starts a list: the list, not the paragraph, holds it
            ("- a\n2. ```\n   x", "   ```"),
            ("- a\n  - b\n  2. ```\n     x", "     ```"),
            # the spec's "1. foo / 2. bar / 3) baz": two lists
            ("1. a\n2. b\n3) ```\n   x", "   ```"),
            # a lone - after prose is a setext underline, not an item
            ("a\n-\n  ```\n  x\n```", None),
            ("a\n+\n  ```\n  x\n```", None),  # and an empty item cannot interrupt a paragraph
            ("- a\n-\n  ```\n  x", "  ```"),  # but as the next item of the list it is one
            # a tab after the marker reaches column 4: the item's gap, and its fence
            ("-\t```\n    x", "    ```"),
            ("-\t```\n  x", None),  # a line below that column ends the item and the block
            ("-\n  ```\n  x\n```", "```"),  # an empty marker line still opens the item
            ("1.\n   ```\n   x\n```", "```"),
            # its column is one past the marker: a closer under it is outside
            ("-\n  ```\n  x\n ```", " ```"),
            ("-   \n  ```\n  x\n```", "```"),  # trailing spaces do not move that column
            ("-  \n  ```\n  x\n ```", " ```"),
            ("1.  \n   ```\n   x\n```", "```"),
            # lazy or not is judged from the enclosing item's column
            ("- a\n  -   b\n    # h\n2. ```\n   x", "   ```"),
            ("- a\n  -   b\n    ```\n    x", "    ```"),
            ("1.\n   ```\n   x\n  ```", "  ```"),
            ("-\n2. ```\n   x", "   ```"),  # an empty item has no paragraph for 2. to continue
            ("- a\n# h\n2. ```\n   x", "   ```"),  # a heading ends the item and its paragraph
            # a quote ends the item: the block then opens at the top level
            ("- a\n> q\n  ```\n  x\n```", None),
            # a heading is not prose, so the line under it is not lazy
            ("- # h\nb\n  ```\n  x\n```", None),
            # but prose behind a quote marker is: the item goes on
            ("- > q\nb\n  ```\n  x\n```", "```"),
            ("- > > a\nb\n  ```\n  x\n```", "```"),  # behind two markers too
            # after the item's block b is not lazy: the item
            ("- ```sh\n  x\n  ```\nb\n     ```\n     y", None),
            ("- ```sh\n  x\nb\n     ```\n     y", None),  # ends, and five spaces are prose
            ("* * *\n  ```sh\n  x\n```", None),  # a thematic break, not three nested items
            ("- - -\n  ```sh\n  x\n```", None),
            # a break inside the item; the block is the item's
            ("- * * *\n  ```sh\n  x\n```", "```"),
            # the break is the item's: four past its column is code
            ("- * * *\n      ```sh\n      x", None),
            # and a closer in the item's window closes
            ("- * * *\n    ```\n    x\n  ```\n  more", None),
            ("```\nx\n```\u00a0", "```"),  # NBSP after a closer is content: not a closer
            ("```\nx\n```\t ", None),  # spaces and tabs are
            # an NBSP line is paragraph text, not a blank line
            ("- a\n\u00a0\n  2. ```\n     x", None),
            # a tab is the next 4-column stop: inside the item
            ("- a\n\t```sh\n\tx\n\n\ty\n\t```\n- b", None),
            # and a column-0 closer ends the item and opens a block
            ("- a\n\t```sh\n\tx\n```", "```"),
            # a tab-indented closer sits at column 4: in the window
            ("- a\n  ```\n  x\n\t```", None),
            ("- a\n  ```\n  x\n\t\t```", "  ```"),  # two tabs: column 8, past it
            ("- \u00a0a\n  ```\n  x\n```", "```"),  # NBSP after the gap is content: the item exists
            ("- \u00a0\nb\n  ```\n  x\n```", "```"),  # an NBSP-only item is not empty: b is lazy
            # an NBSP line is not blank: it ends the item
            ("- a\n  ```\n  x\n\u00a0\n  ```", "  ```"),
            # two of a character are not a break: the paragraph goes on
            ("a\n**\n2. ```\n   x", None),
            # === with no paragraph above is prose: 2. is its text
            ("x\n\n===\n2. ```\n   y", None),
            ("x\n\n--\n2. ```\n   y", None),
            ("===\n2. ```\n   x", None),
            # below the item's column it is a lazy line (spec ex. 93)
            ("- a\n===\n  ```\n  x\n```", "```"),
            ("* * *\r\n  ```sh\r\n  x\r\n```\r", None),  # a CRLF break is a break
            ("a\r\n===\r\n2. ```\r\n   x", "   ```"),  # a CRLF underline is a heading
            ("- a\r\n---\r\n  ```\r\nx\r\n```\r", None),
            ("-\t\t```\n    x", None),  # seven columns after the marker: indented code
            # a non-ASCII digit is not an ordinal: prose, then a fence
            ("\u0967. ```\n   x\n   ```", "   ```"),
            ("\t```\nx", None),  # four columns at the top level: indented code, not a fence
            # two spaces and a tab reach column 4: the item's opener
            ("- a\n  \t```sh\n\t  x", "    ```"),
            # a lazy line keeps the quote's paragraph: 2. is the document's item
            ("> a\nb\n2. ```\n   x", "   ```"),
            ("> a\nb\n> c\n2. ```\n   x", "   ```"),
            ("- > a\n  b\n  2. ```\n     x", "     ```"),
            # a blank line ends the quote's paragraph: b is the document's own
            ("> a\n\nb\n2. ```\n   x", None),
            ("> a\n>\nb\n2. ```\n   x", None),  # a blank quote line too
            ("- > a\n  >\nb\n2. ```\n   x", None),
            ("> a\n- b\n  2. ```\n     x", None),  # an item's content is the item's own paragraph
            # an item's content may start with an item: each marker nests
            ("- - ```\n    x", "    ```"),
            ("1. - ```\n     x", "     ```"),
            ("- - a\n    ```\n    x\n  ```", "  ```"),
            ("-    ```\n     x", "     ```"),  # four spaces after the marker are still its gap
            ("+ ```\n  x", "  ```"),
            ("* ```\n  x", "  ```"),
            ("1234567890. ```\n            x", None),  # ten digits: not a marker
            ("a\n0. ```\n   x", None),  # only 1 interrupts a paragraph
            # the innermost item that holds the line
            ("- a\n  - b\n    - c\n       ```\n       x", "       ```"),
            # yy ends the item (a block, not a paragraph, was open)
            ("- ```sh\n  x\nyy\n  ```\n  z\n```", None),
            # known deviation: a quote is not a container; its fence ends with the quote
            ("> ```\n> x", None),
            ("-\n  2. ```\n     x", "     ```"),  # an empty marker is not prose either
            ("- a\nb\n  ```\n  x\n```", "```"),  # a lazy line does not end the item
            ("- a\nb\n  ```\n  x\nyy", None),
            ("- a\n\n  b\nc\n  ```\n  x\n```", "```"),  # nor after the item's second paragraph
            ("- a\n ```\n x", " ```"),  # a run below the column ends the item: not lazy, a fence
            ("- ```sh\n  a\n\n  b", "  ```"),  # a blank line inside the block keeps it
            ("1. ```\n   a\n   ```\n2. ```\n   b", "   ```"),
            ("- a\n\n  ```\n  x\n```", "```"),
            # a blank line ends the item's paragraph: the line after it is not lazy, and
            # the block is the document's
            ("- a\n\nb\n  ```\n  x\n```", None),
            ("- a\n\nb\n  ```\n  x\nyy", "  ```"),
            # a bare === under the quote's lazy paragraph is lazy text: 2. is the
            # document's item
            ("> a\n===\nb\n2. ```\n   x", "   ```"),
            # === right behind the line's own marker opens the paragraph of that container
            ("- ===\nb\n  ```\n  x\n```", "```"),
            # even under a paragraph of the outer container
            ("a\n- ===\nb\n  ```\n  x\n```", "```"),
            ("1. ===\nb\n   ```\n   x\n```", "```"),
            ("- > ===\nb\n  ```\n  x\n```", "```"),
            ("- ===\n  2. ```\n     x", None),
            ("> ===\nb\n2. ```\n   x", "   ```"),
            ("a\n> ===\nb\n2. ```\n   x", "   ```"),
            ("> a\n> ===\nb\n2. ```\n   x", None),  # a heading inside the quote: b is not lazy
            # an indented code block is not a paragraph: the empty item after it is an item, its
            # fence is the item's, and the dedented line ends both with no closer
            ("    code\n- \n  ```\nfoo", None),
            ("    code\n2. \n   ```\nfoo", None),
            # three spaces are a paragraph: the empty item is its text
            ("   x\n- \n  ```\nfoo", "  ```"),
            ("- # h\n      code\nlazy\n1. \n   ```\n-\nmore", "   ```"),
            # an item that began blank ends at the next blank line: the fence is the document's
            ("- \n\n  ```\nfoo", "  ```"),
            ("-\n\n  ```\nfoo", "  ```"),
            ("1. \n\n   ```\nfoo", "   ```"),
            ("- a\n\n  ```\nfoo", None),  # an item with content survives the blank line
            ("- a\n  - b\n    ```\n    x\n```", "```"),
            ("-     ```\n       x", None),  # five spaces: the item's content is code, not a fence
            ("-     ```\n   ```\n   y\nz", None),  # the item's fence, ended by the dedent
            ("-     x\n   ```\n   y", "   ```"),
            ("-     x\n ```\n y\nz", " ```"),  # one column short of marker + 1 ends the item
            # an underline at the item's own column is a heading there, not a lazy line
            ("- a\n  ===\nb\n  ```\n  code\n```\nafter", None),
            # a fence behind > is code: no line after it is lazy, so the next fence is the
            # document's
            ("> ```\nquoted\n-\n  ```\ntail", "  ```"),
            ("> ```\n> code\nx\n-\n  ```\ntail", "  ```"),
            ("> ```\n> code\n> ```\nlazy\n2. ```\n   y", None),  # closed behind >: 2. is text
            # after the closer the quote's paragraph is prose again: 2. under it is a new list
            ("> ```\n> x\n> ```\n> para\nlazy\n2. ```\n   y", "   ```"),
            # a line with fewer markers ends the quote and its block: the next quote is prose
            ("> ```\nx\n> a\nlazy\n2. ```\n   y", "   ```"),
            ("> ```\n> > x\nlazy\n2. ```\n   y", None),  # more markers: still the block's code
            ("> ```\n>     ```\n> x\nlazy\n2. ```\n   y", None),  # four spaces in: not a closer
            ("- > ```\n> x\nlazy\n2. ```\n   y", "   ```"),  # the item ended: a new quote
            ("> > ```\n> x\nlazy\n2. ```\n   y", "   ```"),  # fewer markers end the inner quote
            ("a\n> ```\n    lazy\n2. ```\n   y", "   ```"),  # the quote's fence ended a's paragraph
            # a line below the opener's item ends it, the quote and the block
            ("- >```\n> >\n  ```\n``", "  ```"),
            ("- > ```\nab> x\n  ```\ncode", "  ```"),
            ("-     \x0cwidget\n  ```\nafter", None),  # a form feed after five spaces: content
            # a wide-gap item's content is indented code even under a paragraph: q is not lazy
            ("p\n-     code\nq\n  ```\nx", "  ```"),
            ("p\n1.     e\nq\n   ```\nx", "   ```"),
            # VT and FF are content, not whitespace: the item exists and holds the fence
            ("- \x0cwidget\n  ```\nafter", None),
            ("- \x0bwidget\n  ```\nafter", None),
            ("1.     x\n  ```\n  y\nz", "  ```"),
            ("* * * \n    ```\n    x", None),  # trailing spaces after a break
            ("- - - \n  ```\n  x\n```", None),
            ("a\n    > b\n2. ```\n   x", None),  # four spaces before >: a lazy line, not a quote
            ("-```\n x", None),  # no space after the marker: not an item
            ("    - ```\n      x", None),  # four spaces before it: indented code
            ("- ```sh\n  x\n2. ```\n   y", "   ```"),  # the item's block ended, so 2. is an item
            ("> a\n2. ```\n   x", "   ```"),
        ):
            with self.subTest(text=text):
                self.assertEqual(_unclosed_fence(text), want)

    def test_limit_below_the_marker_reserve_gets_the_marker_alone(self):
        for limit in (0, _MARKER_RESERVE - 1, _MARKER_RESERVE):  # no room for one character
            self.assertEqual(cut_to_fit("word " * 100, limit), TRUNCATION_MARKER, limit)
        out = cut_to_fit("word " * 100, _MARKER_RESERVE + 1)  # room for exactly one
        self.assertEqual(out, "w\n" + TRUNCATION_MARKER)

    def test_raw_fallback_closer_matches_the_block_left_open_after_the_trim(self):
        pre = "````md\n" + "x" * 120 + "\n````\n"  # a closed 4-tick block, no blank line after it
        text = pre + "```sh\nmake all\n```\n" + "y" * 300
        limit = len(pre) + 4 + _MARKER_RESERVE  # head ends "```s": the 3-tick block is the open one
        out = cut_to_fit(text, limit)
        self.assertLessEqual(len(out), limit)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertIsNone(_unclosed_fence(body), repr(body[-40:]))

    def test_raw_fallback_span_trim_does_not_reopen_a_closed_block(self):
        # head ends exactly on the closer, prose has an odd backtick
        pre = "odd ` tick\n~~~\nls\n~~~"
        out = cut_to_fit(pre + "\n" + "z" * 300, len(pre) + _MARKER_RESERVE)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertIsNone(_unclosed_fence(body), repr(body))
        self.assertIsNone(_open_span(body), repr(body))

    def test_raw_fallback_span_closer_does_not_open_a_fence(self):
        pre = "odd ` tick\n``\n"
        out = cut_to_fit(pre + "z" * 300, len(pre) + _MARKER_RESERVE)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertIsNone(_unclosed_fence(body), repr(body))
        self.assertIsNone(_open_span(body), repr(body))

    def test_raw_fallback_backs_up_to_before_the_open_span(self):
        keep = 60 - _MARKER_RESERVE
        out = cut_to_fit("a" * (keep - 1) + "`" + "z" * 10 + "`" + "z" * 300, 60)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertEqual(body, "a" * (keep - 1) + "\n")  # the span's opener is gone, nothing added

    def test_raw_fallback_drops_a_partial_run_at_the_edge(self):
        keep = 60 - _MARKER_RESERVE
        text = "x" * (keep - 7) + "\n``\n```bash\n" + "y" * 500  # head ends "\n``\n```"
        out = cut_to_fit(text, 60)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertIsNone(_unclosed_fence(body))
        self.assertNotIn("```", body, repr(body[-10:]))  # the cut fence line is gone; "``" is prose
        self.assertIsNone(_open_span(body))

    def test_raw_cut_trims_only_what_the_closer_needs(self):
        self.assertEqual(_raw_cut("```\n" + "x" * 20, 12), "```\nxxxx\n```")
        self.assertEqual(_raw_cut("~~~~\n" + "x" * 20, 14), "~~~~\nxxxx\n~~~~")
        # a prefix ending a line gets no blank line before the closer
        self.assertEqual(_raw_cut("```\nabcdef", 8), "```\n```")
        self.assertEqual(_raw_cut("- a\nb\n  ```sh\n  make all", 20), "- a\nb\n  ```sh\n  ```")

    def test_raw_cut_inside_an_items_opener_line_keeps_nothing(self):
        self.assertEqual(_raw_cut("- ```sh\n  x", 4), "")  # "- ``" is a partial fence line

    def test_a_tab_indented_block_inside_an_item_is_not_cut_open(self):
        item = "- Run it:\n\t```sh\n\tmake all\n\n\tmake test\n\tmake more\n\t```\n"
        text = item + "- next item\n" + "z" * 200
        out = cut_to_fit(text, 60)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertIsNone(_unclosed_fence(body), repr(body))
        # the blank line inside the block is no cut
        self.assertNotIn("make all\n\n" + TRUNCATION_MARKER, out)

    def test_a_run_the_paragraph_never_closes_is_literal_and_stays(self):
        # A stray backtick near the start of a long paragraph used to pull the whole
        # paragraph out of the pack; CommonMark reads an unmatched run as text.
        head = "Plan: edit `foo.py to add the flag, then "
        text = head + "update the parser and the tests, " * 30 + "\n\nStep 2\n"
        out = cut_to_fit(text, 600)
        self.assertGreater(len(out), 500, out)
        self.assertTrue(out.startswith("Plan: edit `foo.py to add the flag, then update"), out[:60])
        # The same run, closed within the budget past the cut: the cut lands inside a real
        # span and backs up. (A closer further away than that is treated as absent: the
        # pack ends the paragraph at the marker, so the run is literal there either way.)
        text = head + "update the parser and the tests, " * 30
        closed = text[:700] + "`" + text[700:]
        self.assertEqual(cut_to_fit(closed, 600), "Plan: edit \n" + TRUNCATION_MARKER)
        self.assertGreater(len(cut_to_fit(text + "z" * 1_000 + "`\n\nStep 2\n", 600)), 500)

    def test_an_open_html_comment_is_a_known_deviation(self):
        # HTML blocks are not tracked (docs/KNOWN_ISSUES.md): the comment stays open across
        # the cut, with the marker still visible to the worker as raw text.
        out = cut_to_fit("<!--\n" + "x " * 100, 60)
        self.assertTrue(out.startswith("<!--\n"), out)
        self.assertNotIn("-->", out)
        # A fence run right under an HTML line is HTML content to CommonMark, and so is the
        # closer appended here: the `<div>` block runs to the blank line the callers add. Only
        # when a blank line inside the walker's block ends the HTML block first does that closer
        # open a fence (docs/KNOWN_ISSUES.md). cmark and GitHub already break such Markdown; the
        # pack does not repair it.
        self.assertEqual(cut_to_fit("<div>\n```\nx", 100), "<div>\n```\nx\n```")

    def test_a_marker_line_setext_lookalike_does_not_cut_a_block_open(self):
        out = cut_to_fit("- ===\nb\n  ```\n  x\n```\n" + "y " * 300, 60)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertIsNone(_unclosed_fence(body), repr(body))
        self.assertFalse(body.rstrip().endswith("\n```\n```"), repr(body))  # no closer that opens
        # An item's fence ended by a dedent needs no closer: appending one would open a block.
        self.assertEqual(cut_to_fit("    code\n- \n  ```\nfoo", 1000), "    code\n- \n  ```\nfoo")
        self.assertEqual(cut_to_fit("- \n\n  ```\nfoo", 1000), "- \n\n  ```\nfoo\n  ```")

    def test_a_section_header_does_not_turn_the_files_first_line_into_its_paragraph(self):
        # Without a blank line after the header, `2. ```` on the file's first line could not
        # interrupt the header's paragraph, and the closer the cut appends would open a block.
        root = _repo({"AGENTS.md": "2. ```\n   code one\n   code two\n   " + "x" * 60})
        text = workspace_instructions(root, budget=90)
        self.assertTrue(text.startswith("--- AGENTS.md ---\n\n2. ```"), text[:40])
        self.assertIsNone(_unclosed_fence(text.split(TRUNCATION_MARKER)[0]), text)

    def test_the_span_check_is_conservative_where_commonmark_rescans(self):
        # Known deviations (docs/KNOWN_ISSUES.md): a pending run hides later spans, a heading
        # line is not rescanned, and a body's last line that continues the paragraph is judged
        # as cut. Each result still reaches the worker closed; pinned so a change is noticed.
        self.assertEqual(_raw_cut("``a `b` c" + "z" * 20, 6), "``a `b")
        self.assertEqual(_raw_cut("# h `a` b" + "z" * 20, 5), "# h `")
        self.assertEqual(_raw_cut("a `b\n---c` d" + "z" * 20, 8), "a `b\n---")
        # A cut inside a line that ends the paragraph in the real text keeps the run as literal.
        self.assertEqual(_raw_cut("x ``\n1. y" + "z" * 20, 7), "x ``\n1.")
        self.assertEqual(_raw_cut("a `\n_ _ _\nb" + "z" * 20, 7), "a `\n_ _")

    def test_a_text_that_fits_but_left_a_block_open_gets_its_closer(self):
        self.assertEqual(cut_to_fit("# Rules\n```sh\nmake", 100), "# Rules\n```sh\nmake\n```")
        self.assertEqual(cut_to_fit("- a\n  ```\n  x", 100), "- a\n  ```\n  x\n  ```")
        self.assertEqual(cut_to_fit("# Rules\n```sh\nmake\n```", 100), "# Rules\n```sh\nmake\n```")
        # no blank line before it
        self.assertEqual(cut_to_fit("```py\ncode\n", 100), "```py\ncode\n```")
        # The closer counts against the limit: a run of 3,000 backticks is not echoed past it.
        out = cut_to_fit("`" * 3_000 + "\ncode", 4_000)
        self.assertLessEqual(len(out), 4_000)
        self.assertIn(TRUNCATION_MARKER, out)
        text = "- step\n  - sub\n    ```bash\n    x"
        self.assertLessEqual(len(cut_to_fit(text, len(text))), len(text))
        self.assertEqual(cut_to_fit(text, len(text) + 8), text + "\n    ```")
        root = _repo({"AGENTS.md": "# Rules\n```sh\nmake\n", "CLAUDE.md": "Real rules.\n"})
        self.assertEqual(
            workspace_instructions(root, budget=100),
            "--- AGENTS.md ---\n\n# Rules\n```sh\nmake\n```\n\n--- CLAUDE.md ---\n\nReal rules.",
        )

    def test_the_break_regex_runs_near_the_lines_end_only(self):
        # Trying it at every nested marker of `- - - ... x` backtracked to the x each
        # time; a 50 KB line of markers walked in seconds.
        spy = mock.Mock(wraps=contextpack._BREAK_LINE)
        with mock.patch.object(contextpack, "_BREAK_LINE", spy):
            self.assertIsNone(_unclosed_fence("- " * 3_900 + "x " + "- " * 100))
        self.assertLessEqual(spy.match.call_count, 8, spy.match.call_count)
        started = time.perf_counter()
        self.assertIsNone(_unclosed_fence("- " * 20_000 + "x"))
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_a_single_huge_line_of_markers_cuts_in_linear_time(self):
        # Each marker is matched at a position; re-slicing the line per marker, and
        # walking a whole line that runs past the budget, took seconds on a 400 KB line.
        for chain in ("> ", "- ", "- " * 3900 + "x "):  # the last: markers that are not a break
            tail = chain * 200_000 + "x" if len(chain) == 2 else chain + "- " * 200_000
            text = "\n" + tail
            started = time.perf_counter()
            out = cut_to_fit(text, 4_000)
            self.assertLess(time.perf_counter() - started, 2.0, chain)
            self.assertLessEqual(len(out), 4_000)

    def test_span_offsets_are_counted_on_the_line_as_written_not_tab_expanded(self):
        self.assertEqual(_open_span("\t`a"), 1)
        self.assertEqual(_raw_cut("x\n\ta `b" + "z" * 5 + "`" + "z" * 20, 12), "x\n\ta ")
        # The paragraph opened by `- \`c` is judged at its own column: `===` under it is a heading.
        self.assertEqual(_raw_cut("1. a\n- `c\n   ===\n   d` e", 21), "1. a\n- `c\n   ===\n   d")

    def test_open_span_with_a_cut_answers_where_that_paragraph_ends(self):
        # the cut starts the heading line: it is read
        self.assertIsNone(_open_span("a `b\n# h `c", 5))
        # the cut is in the paragraph: its run is open
        self.assertEqual(_open_span("a `b\n# h `c", 4), 2)
        self.assertIsNone(_open_span("a `b\n```\nx", 5))  # a fence line ends it the same way
        # A fence line past the cut ends the paragraph: the run it holds open is the answer.
        self.assertEqual(_open_span("a `bcd\n```\nx\n```\nc `d", 2), 2)
        self.assertEqual(_raw_cut("a `bcd\n```\nx\n```\nc `d" + "z" * 30, 6), "a `bcd")

    def test_raw_cut_backs_out_of_a_span_that_a_lazy_quote_line_kept_open(self):
        text = "- > a `\nb\n  > c `d `e" + "z" * 5 + "`" + "z" * 50
        self.assertEqual(_raw_cut(text, 24), "- > a `\nb\n  > c `d ")

    def test_span_tracker_edges(self):
        for text, want in SPAN_CASES:
            with self.subTest(text=text):
                self.assertEqual(_open_span(text), text.rindex("`") if want == "last" else want)
        self.assertEqual([ln.taken for ln in _walk_fences("```foo`\nx")], [False, False])

    def test_raw_cut_never_echoes_a_partial_fence_run(self):
        self.assertEqual(_raw_cut("x" * 8 + "\n```bash\n" + "y" * 50, 16), "x" * 8)
        self.assertEqual(_raw_cut("x" * 8 + "\n~~~\n" + "y" * 50, 11), "x" * 8)  # head ends "\n~~"
        self.assertEqual(_raw_cut("x" * 8 + "\n~~~\n" + "y" * 50, 10), "x" * 8)  # head ends "\n~"

    def test_raw_cut_span_backup_offset_past_the_first_line(self):
        self.assertEqual(_raw_cut("ab\ncd `ef" + "zzz`", 9), "ab\ncd ")

    def test_raw_cut_backs_up_to_the_open_span_not_the_first_one(self):
        self.assertEqual(_raw_cut("`a` `b` `c" + "z" * 5 + "`" + "z" * 20, 10), "`a` `b` ")

    def test_raw_cut_keeps_a_closer_that_ends_exactly_at_keep(self):
        self.assertEqual(_raw_cut("```\nb\n````\nzzz", 10), "```\nb\n````")

    def test_crlf_text_still_cuts_closed(self):
        # The empty-line search assumes LF; CRLF text takes the raw path and
        # still leaves no block open.
        out = cut_to_fit("p\r\n\r\n```sh\r\nx\r\n" + "y" * 300, 60)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertIsNone(_unclosed_fence(body))
        # the closer was appended
        self.assertTrue(body.rstrip().endswith("\n```"), repr(body[-12:]))
        self.assertEqual(body.count("```"), 2)

    def test_raw_fallback_backs_out_of_a_double_backtick_span(self):
        keep = 12
        self.assertEqual(_raw_cut("run ``git `status`` then " + "w" * 300, keep), "run ")

    def test_a_literal_backtick_in_an_earlier_paragraph_does_not_drag_the_cut_back(self):
        out = _raw_cut("a ` b\n\nparagraph two " + "x" * 50, 20)
        self.assertEqual(out, "a ` b\n\nparagraph two")
        out = _raw_cut("a ` b\n \nparagraph two " + "x" * 50, 21)
        self.assertEqual(out, "a ` b\n \nparagraph two")
        out = _raw_cut("a ` b\n```\nx\n```\nc d e f g h\n" + "z" * 50, 24)
        self.assertEqual(out, "a ` b\n```\nx\n```\nc d e f ")

    def test_a_backtick_inside_an_open_block_is_not_a_span(self):
        self.assertEqual(_raw_cut("```\na ` b cdef", 11), "```\na `\n```")

    def test_raw_cut_offsets_survive_a_cr_in_prose(self):
        text = "a\r\nb\r\nc\r\nd `e" + "z" * 5 + "`" + "z" * 20
        self.assertEqual(_raw_cut(text, 15), "a\r\nb\r\nc\r\nd ")

    def test_a_partial_closer_at_keep_is_not_taken_as_a_closer(self):
        # The prefix ends inside a longer closer run; only the real line decides.
        limit = 10 + _MARKER_RESERVE
        for text, want in (
            ("```\nb\n`````\n" + "z" * 50, "```\nb\n```\n"),
            ("~~~\nb\n~~~~~\n" + "z" * 50, "~~~\nb\n~~~\n"),
            ("```\nb\n````x\n" + "z" * 50, "```\nb\n```\n"),
        ):
            with self.subTest(text=text):
                self.assertEqual(cut_to_fit(text, limit), want + TRUNCATION_MARKER)

    def test_a_literal_backtick_before_an_open_block_does_not_drag_the_cut_back(self):
        # The opener line ends the paragraph: the stray backtick is literal, so
        # the block is closed in place rather than the cut pulled back to it.
        self.assertEqual(_raw_cut("a ` b\n```\nxxxxxxxx", 16), "a ` b\n```\nxx\n```")

    def test_a_prefix_ending_flush_before_a_fence_line_keeps_its_newline(self):
        self.assertEqual(_raw_cut("abc\n```bash\nx\n", 4), "abc\n")

    def test_raw_cut_invariants_hold_over_a_fuzz_corpus(self):
        # Deterministic: a fixed seed, a small alphabet rich in fence and span
        # characters, every keep from 1 to 40. Each result is a prefix of the
        # text plus at most a closer, within keep, with no open block, and with
        # no open span other than a run its real paragraph never closes.
        rng = random.Random(7)
        alphabet = [
            "```", "````", "~~~", "  ```", "`", "``", "\n", "\n\n", "\r\n", "a", "b ", "x`y", " ",
            "\n# h", "\n- ", "\n1. ", "\n2. ", "\n===", "\n> ", "\n>", "\n> # h", "\n-*-", "\n---",
            "\n- ```",
            "\n-", "\n1.", "\nlazy", "\n  - ", "\n  > ", "\n- > ", "\n> > ", "\n* * *", "\u00a0",
        ]

        # The oracle restates the CommonMark block rules on its own (the regexes
        # below), but its paragraph loop (depth, laziness, the `cut` return)
        # mirrors `_open_span`, and the fence and list-item state comes from
        # `_walk_fences`: what the fuzz checks is `_raw_cut`'s invariants, not
        # those two, whose defects are pinned by `test_span_tracker_edges` and
        # `test_fence_tracker_edges`. The rules:
        # backticks group into runs per paragraph; a run opens a span and the
        # next run of the same length closes it; a paragraph ends at a blank
        # line (spaces and tabs only), a fence line, the start of an item, an
        # ATX heading, a thematic break of one character, a setext underline,
        # a non-empty list item (ordered only from 1) or a quote opening
        # (more quote markers than the paragraph had), each read from the
        # item's column and from behind the quote markers; a line with fewer
        # markers continues the quote's paragraph while it is open
        # (laziness); a heading, a break or an underline is one line.
        heading = re.compile(r" {0,3}#{1,6}([ \t]|$)")
        brk = re.compile(r" {0,3}([-*_])([ \t]*\1){2,}[ \t]*$")
        underline = re.compile(r" {0,3}(=+|-+)[ \t]*$")
        item = re.compile(r" {0,3}([-+*]|0{0,8}1[.)])[ \t]+\S", re.ASCII)
        quote = re.compile(r" {0,3}> ?")

        def open_run(s: str, cut: int | None = None) -> int | None:
            # The offset of the run left open at the end of `s`, or, with `cut`, at the
            # end of the paragraph holding offset `cut`: a run still open there is literal.
            open_at = None
            open_len = 0
            offset = 0
            depth = 0
            para_open = False
            para_col = 0
            for ln in _walk_fences(s):
                line, fence, taken = ln.text, ln.fence, ln.taken
                column, item_line = ln.column, ln.item
                if taken:  # a fence line ends the paragraph
                    if cut is not None and offset > cut:
                        return open_at
                    open_at = None
                    para_open = False
                elif fence is None:
                    cols = line.rstrip("\r").expandtabs(4)
                    pos, markers = column, 0
                    while (m := quote.match(cols, pos)):
                        pos, markers = m.end(), markers + 1
                    content = cols[pos:]  # the quote's content is prose
                    prose = bool(content.strip(" \t\r"))
                    underlines = (  # under a paragraph of this container only
                        underline.match(content) and para_open
                        and markers >= depth and column >= para_col
                    )
                    starts_block = (
                        heading.match(content) or brk.match(content) or underlines
                        or item.match(content) or markers > depth
                    )
                    lazy = (
                        markers < depth and para_open and prose
                        and not item_line and not starts_block
                    )
                    ends = item_line or not prose or starts_block
                    if ends:
                        if cut is not None and offset > cut:
                            return open_at
                        in_line = cut is not None and offset < cut <= offset + len(line)
                        if in_line and open_at is not None:
                            return open_at
                        open_at = None
                    for m in re.finditer(r"(\\*)(`+)", line):
                        slashes, run = m.group(1), m.group(2)
                        if open_at is None:
                            if len(slashes) % 2:  # an escaped backtick is literal outside a span
                                run = run[1:]
                            if run:
                                open_at, open_len = offset + m.end() - len(run), len(run)
                        elif len(run) == open_len:
                            open_at = None
                    if heading.match(content):
                        open_at = None  # a heading is one line
                    opens = prose and not (  # a quote opening does not close what it opens
                        heading.match(content) or brk.match(content) or underlines
                        or item.match(content)
                    )
                    if opens and (not para_open or ends):
                        para_col = column
                    para_open = opens
                    if not lazy:
                        depth = markers
                offset += len(line) + 1
            return open_at

        for text, want in SPAN_CASES:  # the oracle agrees with `_open_span` where the table pins it
            self.assertEqual(open_run(text), _open_span(text), text)
        for _ in range(400):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(5, 40)))
            for keep in range(1, 41):
                if len(text) <= keep:
                    continue
                out = _raw_cut(text, keep)
                self.assertLessEqual(len(out), keep, (text, keep, out))
                self.assertIsNone(_unclosed_fence(out), (text, keep, out))
                body = out
                if not text.startswith(out):  # a closer was appended on its own line
                    nl = out.rfind("\n")
                    body = out[: nl + 1] if text.startswith(out[: nl + 1]) else out[:nl]
                    self.assertTrue(text.startswith(body), (text, keep, out))
                    self.assertEqual(out[nl + 1 :], _unclosed_fence(body), (text, keep, out))
                run = open_run(body)
                if run is not None:  # only a run its real paragraph never closes may stay open
                    window = text[: len(body) + keep]
                    self.assertEqual(open_run(window, len(body)), run, (text, keep, out))

    def test_cut_to_fit_invariants_hold_over_a_fuzz_corpus(self):
        # The same kind of alphabet through the public entry point, every limit from
        # the reserve up to past the text: within the limit; a prefix plus at most a
        # closer line and the marker (or the text plus at most a closer); no open
        # block. For texts without list markers, quotes or tabs, a second fence
        # tracker written here from the spec (opener: up to three spaces and a run;
        # closer: the same character, at least as long, nothing after it) must also
        # find every block closed, so a walker defect in the container-free case
        # cannot hide behind the walker's own verdict.
        rng = random.Random(11)
        alphabet = [
            "```", "````", "~~~", "  ```", "`", "``", "\n", "\n\n", "a", "b ", "x`y", " ",
            "\n# h", "\n- ", "\n1. ", "\n> ", "\n- ```", "\n    ```", "\n\t```", "\\`",
        ]
        opener = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
        closer = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
        containers = re.compile(r"^ {0,3}([-+*]|\d+[.)])( |$)|^ {0,3}>|\t", re.M)

        def straight_fence(s: str) -> str | None:
            run = None
            for line in s.split("\n"):
                if run is None:
                    m = opener.match(line)
                    if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                        run = m.group(1)
                else:
                    m = closer.match(line)
                    if m and m.group(1)[0] == run[0] and len(m.group(1)) >= len(run):
                        run = None
            return run

        for _ in range(300):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 30)))
            for limit in range(_MARKER_RESERVE + 1, len(text) + _MARKER_RESERVE + 3):
                out = cut_to_fit(text, limit)
                self.assertLessEqual(len(out), limit, (text, limit, out))
                self.assertIsNone(_unclosed_fence(out), (text, limit, out))
                if not containers.search(text):
                    self.assertIsNone(straight_fence(out), (text, limit, out))
                if len(text) > limit:
                    self.assertTrue(out.endswith(TRUNCATION_MARKER), (text, limit, out))
                # (A text that fits, but not with its closer, is cut too.)
                if out.endswith(TRUNCATION_MARKER):
                    body = out[: -len(TRUNCATION_MARKER)].rstrip("\n")  # the cut's own newlines
                else:
                    body = out
                if body and not text.startswith(body):  # a closer line was appended
                    nl = body.rfind("\n")
                    self.assertTrue(text.startswith(body[:nl]), (text, limit, out))
                    self.assertEqual(body[nl + 1 :], _unclosed_fence(body[:nl]), (text, limit, out))

    @unittest.skipUnless(shutil.which("pandoc"), "no pandoc here: the spec is the oracle")
    def test_cut_to_fit_agrees_with_pandoc_on_what_the_marker_lands_in(self):
        # pandoc's commonmark reader is cmark-conformant and is not a dependency: when it is
        # on the machine, each cut result plus a paragraph of its own (what the callers
        # append) goes through it, one file per case (`--file-scope`), and neither the marker
        # nor the sentinel may land in a code block. Left out: block quotes (a documented
        # deviation) and pandoc's own quirk of accepting an escaped backtick in a backtick
        # fence's info string, which the spec and cmark do not, wherever a fence can open
        # (behind list markers too).
        rng = random.Random(5)
        alphabet = [
            "```", "````", "~~~", "  ```", "`", "``", "\n", "\n\n", "a", "b ", "x`y", " ",
            "\n# h", "\n- ", "\n1. ", "\n2. ", "\n===", "\n---", "\n- ```", "\n    ```",
            "\n\t```", "\n-", "\n1.", "\nlazy", "\n  - ", "\n- - ", "\n* * *", "\n    code",
            "\n#", "\\`", "\n1. - ", "\n10. ", "\n+ ", "\n#######", "\\\\`", "\n-     ", "\n* * * ",
        ]
        quirk = re.compile(r"(?m)^[ \t]*(?:(?:[-+*]|\d{1,9}[.)])[ \t]+)*`{3,}[^`\n]*\\`")
        cases: list[tuple[str, int]] = []
        while len(cases) < 300:
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 20)))
            if ">" in text or quirk.search(text.expandtabs(4)):
                continue
            limit = rng.randint(_MARKER_RESERVE + 1, len(text) + _MARKER_RESERVE + 2)
            cases.append((text, limit))
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, (text, limit) in enumerate(cases):
                path = Path(tmp) / f"{i:04d}.md"
                doc = cut_to_fit(text, limit) + f"\n\nSENTINEL{i:04d}\n"
                path.write_text(doc, encoding="utf-8")
                paths.append(str(path))
            html = subprocess.run(
                ["pandoc", "--file-scope", "-f", "commonmark", "-t", "html", *paths],
                capture_output=True, text=True, check=True,
            ).stdout
        lo = 0  # each case is judged on its own output: raw HTML left open must not leak
        for i, (text, limit) in enumerate(cases):
            at = html.find(f"SENTINEL{i:04d}", lo)
            self.assertGreaterEqual(at, 0, (text, limit))
            inside = html.rfind("<pre", lo, at) > html.rfind("</pre>", lo, at)
            self.assertFalse(inside, (text, limit, cut_to_fit(text, limit)))
            lo = at

    def test_workspace_instructions_uses_it(self):
        root = _repo({"AGENTS.md": self.FIXTURE})
        text = workspace_instructions(root, budget=150)
        self.assertIn(TRUNCATION_MARKER, text)
        self.assertTrue(text.split(TRUNCATION_MARKER)[0].endswith("## Gate\n\n"), text)


class WorkspaceInstructionsBudget(unittest.TestCase):
    """`workspace_instructions` spends one budget across the instruction files:
    each file is cut against what is left, and a file met with no room for a
    single character before the marker is left out, not reduced to the marker.
    """

    def test_a_second_file_is_skipped_when_less_than_the_reserve_is_left(self):
        # 15 left, below the reserve of 20
        root = _repo({"AGENTS.md": "a" * 85, "CLAUDE.md": "b" * 50})
        text = workspace_instructions(root, budget=100)
        self.assertNotIn("CLAUDE.md", text)
        self.assertNotIn(TRUNCATION_MARKER, text)

    def test_a_second_file_is_cut_against_what_is_left_not_the_whole_budget(self):
        root = _repo({"AGENTS.md": "a" * 60, "CLAUDE.md": "b" * 200})
        text = workspace_instructions(root, budget=100)
        self.assertIn("--- CLAUDE.md ---", text)
        self.assertIn(TRUNCATION_MARKER, text)
        bodies = len(text) - len("--- AGENTS.md ---\n\n") - len("\n\n--- CLAUDE.md ---\n\n")
        self.assertLessEqual(bodies, 100)

    def test_a_second_file_is_skipped_when_exactly_the_reserve_is_left(self):
        root = _repo({"AGENTS.md": "a" * (100 - _MARKER_RESERVE), "CLAUDE.md": "b" * 50})
        text = workspace_instructions(root, budget=100)
        self.assertNotIn("CLAUDE.md", text)
        self.assertNotIn(TRUNCATION_MARKER, text)

    def test_instructions_over_the_budget_by_a_few_characters_are_cut(self):
        root = _repo({"AGENTS.md": "a " * 52})  # 104 chars, budget 100
        text = workspace_instructions(root, budget=100)
        self.assertLessEqual(len(text) - len("--- AGENTS.md ---\n\n"), 100)
        self.assertIn(TRUNCATION_MARKER, text)


if __name__ == "__main__":
    unittest.main()
