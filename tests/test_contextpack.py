"""Tests for the context pack's #3 enrichment: import-graph slice + test contract.

The base pack scores files by keyword and emits full/skeleton blocks. #3 adds,
for the top-scored TARGET files, their import-graph neighbors (what a target
imports and who imports it) and the target's sibling TEST files — so the worker
sees the structural neighborhood and the test contract without blind exploration.

All deterministic, pure stdlib: same repo state + feature => byte-identical pack.
Runnable with: python -m unittest tests.test_contextpack
"""

from __future__ import annotations

import random
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev.contextpack import (  # noqa: E402
    _MARKER_RESERVE,
    _open_span,
    _raw_cut,
    _walk_fences,
    _unclosed_fence,
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

    def test_plan_text_over_the_cap_is_cut_and_marked(self):
        root = _repo({"a.py": "x = 1\n"})
        pack, _ = build_pack(root, "a thing", plan_text="x" * (PLAN_TEXT_CAP + 500))
        self.assertIn(TRUNCATION_MARKER, pack)
        kept = pack.split("FEATURE PLAN", 1)[1].split("\n", 1)[1].split(TRUNCATION_MARKER, 1)[0]
        # The cap covers the whole section: the plan that survived, its
        # newline and the marker.
        self.assertLessEqual(len(kept) + len(TRUNCATION_MARKER), PLAN_TEXT_CAP)

    def test_plan_text_cut_backs_out_of_an_unclosed_fence(self):
        root = _repo({"a.py": "x = 1\n"})
        prose = "p " * (PLAN_TEXT_CAP * 3 // 10)  # the blank line before the block sits in the second half
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
        self.assertEqual(out.split(TRUNCATION_MARKER)[0], "a" * (keep - 1) + "\n\n")  # raw prefix, then the join

    def test_an_empty_raw_prefix_yields_the_marker_alone(self):
        self.assertEqual(cut_to_fit("`" + "x" * 100, _MARKER_RESERVE + 1), TRUNCATION_MARKER)

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
        text = intro + "\n\n```bash\nstep one\n\nstep two\n\nstep three\n\nstep four\n```\n\nAfter.\n"
        self.assertGreater(len(text), 110)  # the limit lands inside the block
        out = cut_to_fit(text, 110)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertEqual(body, intro + "\n\n", repr(body))

    def test_one_long_paragraph_falls_back_to_the_raw_cut(self):
        out = cut_to_fit("R" * 50_000, 4_000)
        self.assertEqual(len(out), 4_000)
        self.assertTrue(out.endswith("\n" + TRUNCATION_MARKER))

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
            ("   ~~~\nx", "~~~"),
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
        pre = "odd ` tick\n~~~\nls\n~~~"  # head ends exactly on the closer, prose has an odd backtick
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
        out = cut_to_fit("a" * (keep - 1) + "`" + "z" * 300, 60)
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

    def test_span_tracker_edges(self):
        cases = (
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
            ("a ` b\n```\nx", None),  # an OPENER ends the paragraph too, even with the block still open
        )
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(_open_span(text), text.rindex("`") if want == "last" else want)
        self.assertEqual([m for _, _, m in _walk_fences("```foo`\nx")], [False, False])

    def test_raw_cut_never_echoes_a_partial_fence_run(self):
        self.assertEqual(_raw_cut("x" * 8 + "\n```bash\n" + "y" * 50, 16), "x" * 8)
        self.assertEqual(_raw_cut("x" * 8 + "\n~~~\n" + "y" * 50, 11), "x" * 8)  # head ends "\n~~"
        self.assertEqual(_raw_cut("x" * 8 + "\n~~~\n" + "y" * 50, 10), "x" * 8)  # head ends "\n~"

    def test_raw_cut_span_backup_offset_past_the_first_line(self):
        self.assertEqual(_raw_cut("ab\ncd `ef" + "zzz", 9), "ab\ncd ")

    def test_raw_cut_backs_up_to_the_open_span_not_the_first_one(self):
        self.assertEqual(_raw_cut("`a` `b` `c" + "z" * 20, 10), "`a` `b` ")

    def test_raw_cut_keeps_a_closer_that_ends_exactly_at_keep(self):
        self.assertEqual(_raw_cut("```\nb\n````\nzzz", 10), "```\nb\n````")

    def test_crlf_text_still_cuts_closed(self):
        # The empty-line search assumes LF; CRLF text takes the raw path and
        # still leaves no block open.
        out = cut_to_fit("p\r\n\r\n```sh\r\nx\r\n" + "y" * 300, 60)
        body = out.split(TRUNCATION_MARKER)[0]
        self.assertIsNone(_unclosed_fence(body))
        self.assertTrue(body.rstrip().endswith("\n```"), repr(body[-12:]))  # the closer was appended
        self.assertEqual(body.count("```"), 2)

    def test_raw_fallback_backs_out_of_a_double_backtick_span(self):
        keep = 12
        self.assertEqual(_raw_cut("run ``git `status``` then " + "w" * 300, keep), "run ")

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
        self.assertEqual(_raw_cut("a\r\nb\r\nc\r\nd `e" + "z" * 20, 15), "a\r\nb\r\nc\r\nd ")

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
        # text plus at most a closer, within keep, with no open block or span.
        rng = random.Random(7)
        alphabet = ["```", "````", "~~~", "`", "``", "\n", "\n\n", "\r\n", "a", "b ", "x`y", " "]

        def spans_closed(out: str) -> bool:
            # Independent restatement of the CommonMark span rule: group
            # backticks into runs per paragraph (a blank line or a fence line
            # ends one); a run opens a span and the next run of the same
            # length closes it. The fence state itself comes from
            # `_walk_fences`, so walker defects are not visible here;
            # `test_fence_tracker_edges` and `test_span_tracker_edges` pin
            # those.
            open_len = 0
            for line, opener, matched in _walk_fences(out):
                if matched:  # a fence line ends the paragraph
                    open_len = 0
                    continue
                if opener is not None:
                    continue
                if not line.strip():
                    open_len = 0
                for run in re.findall(r"`+", line):
                    if open_len == 0:
                        open_len = len(run)
                    elif len(run) == open_len:
                        open_len = 0
            return open_len == 0

        for _ in range(400):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(5, 40)))
            for keep in range(1, 41):
                if len(text) <= keep:
                    continue
                out = _raw_cut(text, keep)
                self.assertLessEqual(len(out), keep, (text, keep, out))
                self.assertIsNone(_unclosed_fence(out), (text, keep, out))
                self.assertTrue(spans_closed(out), (text, keep, out))
                opener = _unclosed_fence(out[: out.rfind("\n")]) if "\n" in out else None
                prefix = out if opener is None else out[: -len("\n" + opener)]
                self.assertTrue(text.startswith(prefix), (text, keep, out))

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
        root = _repo({"AGENTS.md": "a" * 85, "CLAUDE.md": "b" * 50})  # 15 left, below the reserve of 20
        text = workspace_instructions(root, budget=100)
        self.assertNotIn("CLAUDE.md", text)
        self.assertNotIn(TRUNCATION_MARKER, text)

    def test_a_second_file_is_cut_against_what_is_left_not_the_whole_budget(self):
        root = _repo({"AGENTS.md": "a" * 60, "CLAUDE.md": "b" * 200})
        text = workspace_instructions(root, budget=100)
        self.assertIn("--- CLAUDE.md ---", text)
        self.assertIn(TRUNCATION_MARKER, text)
        bodies = len(text) - len("--- AGENTS.md ---\n") - len("\n\n--- CLAUDE.md ---\n")
        self.assertLessEqual(bodies, 100)

    def test_a_second_file_is_skipped_when_exactly_the_reserve_is_left(self):
        root = _repo({"AGENTS.md": "a" * (100 - _MARKER_RESERVE), "CLAUDE.md": "b" * 50})
        text = workspace_instructions(root, budget=100)
        self.assertNotIn("CLAUDE.md", text)
        self.assertNotIn(TRUNCATION_MARKER, text)

    def test_instructions_over_the_budget_by_a_few_characters_are_cut(self):
        root = _repo({"AGENTS.md": "a " * 52})  # 104 chars, budget 100
        text = workspace_instructions(root, budget=100)
        self.assertLessEqual(len(text) - len("--- AGENTS.md ---\n"), 100)
        self.assertIn(TRUNCATION_MARKER, text)


if __name__ == "__main__":
    unittest.main()
