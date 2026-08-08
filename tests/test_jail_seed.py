"""`jail_seed`: a per-run copy of a warm cache, so the worker's toolchain
starts warm without a shared directory anyone can race on.

`jail_env` (0.1.32) let a repo point its toolchain at a cache outside the
clone. For a dependency cache that is enough — it is only read. A BUILD cache
is written, so pointing every run at one shared directory means two writers
whenever the human compiles locally while a worker runs.

Seeding removes the race without losing the warm start. Measured on a real
Phoenix repo: 38.24s cold; `cp -c` of the 56M warm build takes 0.49s (APFS
clone, copy-on-write) and compiling from the seeded copy with the source at a
NEW path — which is what every jail is — takes 4.76s. ~5.3s against 38s, and
the origin is never written.

Runnable with: python -m unittest tests.test_jail_seed
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _repo(config):
    from tmpdirs import mkdtemp

    root = Path(mkdtemp(prefix="shepherd-jailseed-"))
    if config is not None:
        (root / ".shepherd-dev.json").write_text(json.dumps(config), encoding="utf-8")
    return root


def _warm(files: dict[str, str]) -> Path:
    from tmpdirs import mkdtemp

    src = Path(mkdtemp(prefix="shepherd-warm-"))
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return src


class JailSeedConfigTests(unittest.TestCase):
    def test_absent_config_is_empty(self):
        from shepherd_dev.config import jail_seed

        self.assertEqual(jail_seed(_repo(None)), {})
        self.assertEqual(jail_seed(_repo({"test_cmd": "mix test"})), {})

    def test_a_tilde_origin_is_expanded(self):
        from shepherd_dev.config import jail_seed

        got = jail_seed(_repo({"jail_seed": {"MIX_BUILD_ROOT": "~/.cache/app-build"}}))
        self.assertTrue(got["MIX_BUILD_ROOT"].startswith(str(Path.home())))

    def test_non_string_and_refused_names_are_dropped(self):
        """Same refusal list as jail_env: this sets an environment variable
        too, so it must not be a way around that guard."""
        from shepherd_dev.config import jail_seed

        got = jail_seed(_repo({"jail_seed": {
            "CARGO_TARGET_DIR": "/warm", "PYTHONPATH": "/evil", "BAD": 7,
        }}))
        self.assertEqual(got, {"CARGO_TARGET_DIR": "/warm"})

    def test_a_non_object_is_ignored(self):
        from shepherd_dev.config import jail_seed

        self.assertEqual(jail_seed(_repo({"jail_seed": ["MIX_BUILD_ROOT"]})), {})


class SeedingTests(unittest.TestCase):
    def setUp(self):
        self._before = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._before)))

    def test_the_variable_points_at_a_copy_holding_the_warm_content(self):
        from shepherd_dev.config import jail_seed_applied

        src = _warm({"lib/app.beam": "compiled", "nested/deep/x.beam": "also"})
        repo = _repo({"jail_seed": {"MIX_BUILD_ROOT": str(src)}})
        with jail_seed_applied(repo):
            seeded = Path(os.environ["MIX_BUILD_ROOT"])
            self.assertNotEqual(seeded, src, "a copy, not the origin itself")
            self.assertEqual((seeded / "lib/app.beam").read_text(), "compiled")
            self.assertEqual((seeded / "nested/deep/x.beam").read_text(), "also")

    def test_writing_to_the_copy_never_reaches_the_origin(self):
        """The whole point: the origin stays a clean warm baseline."""
        from shepherd_dev.config import jail_seed_applied

        src = _warm({"a.beam": "v1"})
        repo = _repo({"jail_seed": {"MIX_BUILD_ROOT": str(src)}})
        with jail_seed_applied(repo):
            seeded = Path(os.environ["MIX_BUILD_ROOT"])
            (seeded / "a.beam").write_text("v2 — the worker rebuilt it")
            (seeded / "new.beam").write_text("and added this")
        self.assertEqual((src / "a.beam").read_text(), "v1")
        self.assertFalse((src / "new.beam").exists())

    def test_two_runs_get_different_copies(self):
        from shepherd_dev.config import jail_seed_applied

        src = _warm({"a.beam": "v1"})
        repo = _repo({"jail_seed": {"MIX_BUILD_ROOT": str(src)}})
        seen = []
        for _ in range(2):
            with jail_seed_applied(repo):
                seen.append(os.environ["MIX_BUILD_ROOT"])
        self.assertNotEqual(seen[0], seen[1], "concurrent lanes must not share one")

    def test_the_copy_is_removed_afterwards(self):
        from shepherd_dev.config import jail_seed_applied

        src = _warm({"a.beam": "v1"})
        repo = _repo({"jail_seed": {"MIX_BUILD_ROOT": str(src)}})
        with jail_seed_applied(repo):
            seeded = Path(os.environ["MIX_BUILD_ROOT"])
        self.assertFalse(seeded.exists())
        self.assertNotIn("MIX_BUILD_ROOT", os.environ)

    def test_it_is_removed_even_when_the_block_raises(self):
        from shepherd_dev.config import jail_seed_applied

        src = _warm({"a.beam": "v1"})
        repo = _repo({"jail_seed": {"MIX_BUILD_ROOT": str(src)}})
        seen = {}
        with self.assertRaises(RuntimeError):
            with jail_seed_applied(repo):
                seen["p"] = os.environ["MIX_BUILD_ROOT"]
                raise RuntimeError("boom")
        self.assertFalse(Path(seen["p"]).exists())

    def test_a_missing_origin_still_gives_an_empty_directory(self):
        """A first run, before any warm cache exists, must not fail — the
        toolchain simply builds cold into it and the run proceeds."""
        from shepherd_dev.config import jail_seed_applied

        repo = _repo({"jail_seed": {"MIX_BUILD_ROOT": "/nonexistent/warm-cache"}})
        with jail_seed_applied(repo):
            seeded = Path(os.environ["MIX_BUILD_ROOT"])
            self.assertTrue(seeded.is_dir())
            self.assertEqual(list(seeded.iterdir()), [])

    def test_an_empty_config_is_a_no_op(self):
        from shepherd_dev.config import jail_seed_applied

        before = dict(os.environ)
        with jail_seed_applied(_repo(None)):
            self.assertEqual(dict(os.environ), before)

    def test_a_pre_existing_value_is_restored(self):
        from shepherd_dev.config import jail_seed_applied

        os.environ["MIX_BUILD_ROOT"] = "/mine"
        src = _warm({"a.beam": "v1"})
        with jail_seed_applied(_repo({"jail_seed": {"MIX_BUILD_ROOT": str(src)}})):
            self.assertNotEqual(os.environ["MIX_BUILD_ROOT"], "/mine")
        self.assertEqual(os.environ["MIX_BUILD_ROOT"], "/mine")


class CloneFallbackTests(unittest.TestCase):
    """`cp -c` is an APFS clone: 56M in half a second, no disk until it
    diverges. Only APFS has it, so a plain copy has to work everywhere else."""

    def test_a_filesystem_without_clonefile_still_copies(self):
        import shepherd_dev.config as C

        src = _warm({"a.beam": "v1", "d/b.beam": "v2"})
        dest = Path(str(src) + "-dest")
        self.addCleanup(lambda: __import__("shutil").rmtree(dest, ignore_errors=True))

        real = C._clone_dir_fast
        C._clone_dir_fast = lambda s, d: False  # pretend clonefile is unavailable
        try:
            C._copy_tree(src, dest)
        finally:
            C._clone_dir_fast = real
        self.assertEqual((dest / "a.beam").read_text(), "v1")
        self.assertEqual((dest / "d/b.beam").read_text(), "v2")


class CommandsSeedTests(unittest.TestCase):
    """Wiring, not plumbing. A helper nobody calls passes every test above."""

    def setUp(self):
        self._before = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._before)))

    def test_run_seeds_before_the_command_body(self):
        import shepherd_dev.cli as C

        src = _warm({"a.beam": "warm"})
        repo = _repo({"jail_seed": {"MIX_BUILD_ROOT": str(src)}})
        (repo / ".vcscore").mkdir()
        seen = {}

        def _inner(args, repo_root):
            root = os.environ.get("MIX_BUILD_ROOT")
            seen["path"] = root
            seen["content"] = (Path(root) / "a.beam").read_text() if root else None
            return 0

        real, real_opt = C._cmd_run_inner, C._maybe_optimize_after
        C._cmd_run_inner = _inner
        C._maybe_optimize_after = lambda *a, **k: None
        try:
            C.cmd_run(type("A", (), {"repo": str(repo)})())
        finally:
            C._cmd_run_inner, C._maybe_optimize_after = real, real_opt

        self.assertEqual(seen["content"], "warm", "the copy must be warm inside the run")
        self.assertNotEqual(seen["path"], str(src), "and must not be the origin")
        self.assertFalse(Path(seen["path"]).exists(), "and must not outlive the run")


if __name__ == "__main__":
    unittest.main()
