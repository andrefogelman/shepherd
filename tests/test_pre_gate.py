"""`pre_gate_cmd`: run a fixer over the proposal before the gate judges it.

Seed (sac, 2026-08-08): a repo's gate runs `mix format --check-formatted`
before the suite. Attempt 1 died on a missing blank line. The worker was told,
fixed it, and attempt 3 died on the SAME class again — so the run ended with
no proposal, two of its three attempts spent on whitespace. Guidance does not
hold this class; the worker reintroduces it.

Measured: `mix format` fixes it in 0.56s, against 15-47 minutes for the
attempt it costs. Running the fixer as part of the gate makes the class
structurally impossible instead of relying on the worker to remember.

Runnable with: python -m unittest tests.test_pre_gate
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _repo(config=None, files=None):
    from tmpdirs import mkdtemp

    root = Path(mkdtemp(prefix="shepherd-pregate-"))
    for rel, text in (files or {"README.md": "x\n"}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if config is not None:
        (root / ".shepherd-dev.json").write_text(json.dumps(config), encoding="utf-8")
    return root


class PreGateConfigTests(unittest.TestCase):
    def test_absent_is_none(self):
        from shepherd_dev.config import pre_gate_cmd

        self.assertIsNone(pre_gate_cmd(_repo()))
        self.assertIsNone(pre_gate_cmd(_repo({"test_cmd": "mix test"})))

    def test_a_declared_command_comes_back(self):
        from shepherd_dev.config import pre_gate_cmd

        self.assertEqual(pre_gate_cmd(_repo({"pre_gate_cmd": "mix format"})), "mix format")

    def test_a_non_string_is_ignored(self):
        from shepherd_dev.config import pre_gate_cmd

        for bad in (["mix", "format"], 3, ""):
            with self.subTest(value=bad):
                self.assertIsNone(pre_gate_cmd(_repo({"pre_gate_cmd": bad})))


class RunPreGateTests(unittest.TestCase):
    def _run(self, cmd, entries, files=None, timeout=60):
        from shepherd_dev.supervisor import run_pre_gate

        return run_pre_gate(_repo(files=files), entries, cmd, timeout=timeout)

    def test_a_fixer_rewriting_a_proposed_file_updates_the_proposal(self):
        entries = {"a.py": b"x=1\n"}
        updated, err = self._run("printf 'x = 1\\n' > a.py", entries)
        self.assertIsNone(err)
        self.assertEqual(updated["a.py"], b"x = 1\n")

    def test_a_file_the_fixer_leaves_alone_is_unchanged(self):
        entries = {"a.py": b"x = 1\n", "b.py": b"y = 2\n"}
        updated, _ = self._run("printf 'x = 1  # touched\\n' > a.py", entries)
        self.assertEqual(updated["b.py"], b"y = 2\n")

    def test_the_fixer_may_not_widen_the_proposal(self):
        """`mix format` with no arguments formats the WHOLE project. Collecting
        everything it touched would turn a 7-file proposal into a 200-file one
        and hand the human a diff nobody asked for. Only the paths the worker
        proposed are read back."""
        entries = {"a.py": b"x=1\n"}
        updated, _ = self._run(
            "printf 'x = 1\\n' > a.py; printf 'reformatted\\n' > untouched.py",
            entries,
            files={"untouched.py": "original\n"},
        )
        self.assertEqual(set(updated), {"a.py"}, "scope is the worker's, not the fixer's")

    def test_a_proposed_file_the_fixer_deletes_keeps_its_proposed_content(self):
        """A fixer is not a place to drop files from a proposal."""
        entries = {"a.py": b"x = 1\n"}
        updated, _ = self._run("rm a.py", entries)
        self.assertEqual(updated["a.py"], b"x = 1\n")

    def test_a_failing_fixer_reports_and_changes_nothing(self):
        entries = {"a.py": b"x=1\n"}
        updated, err = self._run("printf 'x = 1\\n' > a.py; exit 3", entries)
        self.assertIsNotNone(err)
        self.assertIn("3", err)
        self.assertEqual(updated, entries, "a failed fixer must not half-apply")

    def test_a_hanging_fixer_times_out_without_killing_the_run(self):
        entries = {"a.py": b"x=1\n"}
        updated, err = self._run("sleep 30", entries, timeout=1)
        self.assertIsNotNone(err)
        self.assertIn("timed out", err.lower())
        self.assertEqual(updated, entries)

    def test_the_repo_itself_is_never_written(self):
        """It runs on a materialised copy — the worktree is untouched, same as
        the gate."""
        from shepherd_dev.supervisor import run_pre_gate

        root = _repo(files={"a.py": "original\n"})
        run_pre_gate(root, {"a.py": b"x=1\n"}, "printf 'CLOBBERED\\n' > a.py", timeout=60)
        self.assertEqual((root / "a.py").read_text(), "original\n")


class DevelopWiringTests(unittest.TestCase):
    """The fixer has to run BEFORE the gate and the gate has to judge what it
    produced — otherwise it fixes a tree nobody tests."""

    def _run(self, pre_gate=None):
        from shepherd_dev import supervisor as sup

        seen = {"gate_entries": None, "pre_gate_calls": 0}

        class _Output:
            def changeset(self):
                return {"a.py": b"x=1\n"}

            def discard(self):
                pass

        class _Run:
            run_ref = "run-1"

            def output(self):
                return _Output()

        class _Tasks:
            def register(self, task):
                pass

        class _Workspace:
            tasks = _Tasks()

            def run(self, task, **kw):
                return _Run()

        def _gate(repo_root, entries, test_cmd, timeout, **kw):
            seen["gate_entries"] = dict(entries)
            return sup.GateResult(True, 0, "ok")

        def _pre(repo_root, entries, cmd, timeout=120):
            seen["pre_gate_calls"] += 1
            return {rel: b"x = 1\n" for rel in entries}, None

        orig = (sup.read_changeset_entries, sup._run_gate, sup.run_pre_gate,
                sup._start_gate_warmup)
        sup.read_changeset_entries = dict
        sup._run_gate = _gate
        sup.run_pre_gate = _pre
        sup._start_gate_warmup = lambda *a, **k: None
        try:
            report = sup.develop(
                _Workspace(), object(), repo=object(), repo_root=Path("/r"),
                feature="f", test_cmd="pytest -q", max_attempts=1,
                pre_gate_cmd=pre_gate,
            )
        finally:
            (sup.read_changeset_entries, sup._run_gate, sup.run_pre_gate,
             sup._start_gate_warmup) = orig
        return report, seen

    def test_without_the_config_nothing_extra_runs(self):
        report, seen = self._run(pre_gate=None)
        self.assertEqual(seen["pre_gate_calls"], 0)
        self.assertEqual(seen["gate_entries"], {"a.py": b"x=1\n"})

    def test_the_gate_judges_the_fixed_content(self):
        report, seen = self._run(pre_gate="mix format")
        self.assertEqual(seen["pre_gate_calls"], 1)
        self.assertEqual(seen["gate_entries"], {"a.py": b"x = 1\n"})

    def test_the_proposal_that_reaches_settle_is_the_fixed_one(self):
        """What the gate tested and what the human settles must be the same
        bytes — otherwise the gate's verdict is about a tree that never ships."""
        report, _ = self._run(pre_gate="mix format")
        self.assertEqual(report.entries, {"a.py": b"x = 1\n"})


if __name__ == "__main__":
    unittest.main()
