"""The prompt the jailed worker reads is rendered by shepherd-dev, as a document.

Before: the substrate's workspace-control envelope — the whole of tasks.py as a
fenced source block, then the arguments as `json.dumps(indent=2)` with
ensure_ascii on. A 25k-char context pack arrived as one line of `\\n` escapes
with every accent spelled `\\u00e7`, labelled `guidance`, which the prompt
itself defined as "feedback from a previous failed attempt". Nothing pinned
that shape, so nothing noticed.

Runnable with: python -m unittest tests.test_promptrender
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.promptrender import (  # noqa: E402
    CLOSING,
    TASK_SECTIONS,
    fence,
    prompt_summary,
    render_prompt,
    render_section,
    task_key,
)
from shepherd_dev.prompts import DEFAULT_PROMPTS  # noqa: E402

PACK = (
    "CONTEXT PACK (pre-computed locally):\n"
    "== REPO FILE TREE ==\nlib/a.ex\nlib/b.ex\n"
    "== FILE: lib/a.ex (full) ==\ndefmodule A do\n  def x, do: \"ção\"\nend\n"
)
FEATURE = "Adicionar validação de CPF\n\nStep 1: criar Sac.CPF.valido?/1"


class TaskKeyTests(unittest.TestCase):
    def test_our_tasks_map_to_their_prompt_keys(self):
        self.assertEqual(task_key("shepherd_dev.tasks.implement"), "implement")
        self.assertEqual(task_key("shepherd_dev.tasks.write_tests"), "write_tests")
        self.assertEqual(task_key("shepherd_dev.tasks.review"), "review")

    def test_anything_else_is_not_ours(self):
        self.assertIsNone(task_key("shepherd_dev.tasks.smoke_change"))
        self.assertIsNone(task_key("other.package.implement"))
        self.assertIsNone(task_key(""))
        self.assertIsNone(task_key(None))  # type: ignore[arg-type]

    def test_every_section_list_names_a_prompt_that_exists(self):
        for key in TASK_SECTIONS:
            self.assertIn(key, DEFAULT_PROMPTS)
            self.assertIn(key, CLOSING)


class RenderSectionTests(unittest.TestCase):
    def test_single_line_stays_inline(self):
        self.assertEqual(render_section("gate", "mix test"), "## gate\nmix test")

    def test_multi_line_is_fenced_verbatim(self):
        out = render_section("context", PACK)
        self.assertTrue(out.startswith("## context\n```text\n"))
        self.assertIn(PACK.strip("\n"), out)
        self.assertNotIn("\\n", out)
        self.assertIn("ção", out)

    def test_the_fence_outgrows_any_backtick_run_inside(self):
        value = "line\n````\nstill inside\n"
        out = render_section("diff", value)
        self.assertTrue(out.startswith("## diff\n`````text\n"), out[:40])
        self.assertEqual(fence(value), "`````")

    def test_empty_values_render_nothing(self):
        self.assertIsNone(render_section("guidance", ""))
        self.assertIsNone(render_section("guidance", "   \n"))
        self.assertIsNone(render_section("guidance", None))


class RenderPromptTests(unittest.TestCase):
    def test_the_worker_prompt_is_a_document_not_an_envelope(self):
        out = render_prompt(
            "shepherd_dev.tasks.implement",
            {"repo": object(), "feature": FEATURE, "context": PACK, "guidance": "", "gate": "mix test"},
            fallback="ENVELOPE",
        )
        # the task's instructions come first, dedented, and nothing of the
        # substrate's envelope survives
        self.assertTrue(out.startswith("Implement the requested feature in the repository.\n\nRequirements:\n- Follow"))
        self.assertNotIn("ENVELOPE", out)
        self.assertNotIn("Inputs:", out)
        self.assertNotIn("Task contract source", out)
        self.assertNotIn("Respond with JSON", out)
        # values are verbatim: real newlines, real accents
        self.assertNotIn("\\n", out)
        self.assertNotIn("\\u00e7", out)
        self.assertIn("ção", out)
        # sections, in the documented order; the empty guidance is absent
        order = [out.index(f"## {name}") for name in ("context", "feature", "gate")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("## guidance", out)
        self.assertIn("## gate\nmix test", out)
        # the pack is under its own name — never under "guidance"
        self.assertIn("## context\n", out)
        self.assertIn("== FILE: lib/a.ex (full) ==", out)
        # and the repo object is not rendered at all: the cwd is the repo
        self.assertNotIn("<object object", out)
        self.assertTrue(out.rstrip().endswith(CLOSING["implement"]))

    def test_guidance_appears_on_a_retry(self):
        out = render_prompt(
            "shepherd_dev.tasks.implement",
            {"feature": "f", "guidance": "PREVIOUS ATTEMPT: failed the test suite (exit 1).\nfix it"},
            fallback="",
        )
        self.assertIn("## guidance\n```text\nPREVIOUS ATTEMPT", out)

    def test_the_review_prompt_carries_every_reviewer_input(self):
        out = render_prompt(
            "shepherd_dev.tasks.review",
            {
                "feature": "f",
                "diff": "=== CHANGED FILES (1) ===\nlib/a.ex\n--- a/lib/a.ex\n+++ b/lib/a.ex\n-old\n+new\n",
                "context": PACK,
                "findings": "- [abc123def456] cache never invalidated",
                "lens": "security",
                "proposed_root": "/tmp/proposed",
                "gate": "mix test\nPASSED (exit 0)",
            },
            fallback="",
        )
        self.assertTrue(out.startswith("Review a proposed change to this repository."))
        for name in ("context", "diff", "feature", "findings", "lens", "proposed_root", "gate"):
            self.assertIn(f"## {name}", out)
        self.assertIn("## proposed_root\n/tmp/proposed", out)
        self.assertIn("-old\n+new", out)
        self.assertTrue(out.rstrip().endswith(CLOSING["review"]))

    def test_a_task_that_is_not_ours_keeps_the_substrate_prompt(self):
        self.assertEqual(
            render_prompt("shepherd_dev.tasks.smoke_change", {"output_path": "x"}, fallback="ENVELOPE"),
            "ENVELOPE",
        )

    def test_summary_describes_shape_not_content(self):
        kwargs = {"feature": FEATURE, "context": PACK, "guidance": "", "gate": "mix test"}
        rendered = render_prompt("shepherd_dev.tasks.implement", kwargs, fallback="")
        summary = prompt_summary("shepherd_dev.tasks.implement", kwargs, rendered)
        self.assertEqual(summary["task"], "implement")
        self.assertTrue(summary["rendered"])
        self.assertEqual(summary["chars"], len(rendered))
        self.assertEqual(summary["sections"], ["context", "feature", "gate"])
        self.assertNotIn(PACK, str(summary))


try:
    from shepherd_dialect.workspace_control import runtime_provider as _rp

    _HAS_SUBSTRATE = True
except Exception:  # pragma: no cover - substrate absent
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class TheTransportSendsTheRenderedPrompt(unittest.TestCase):
    """set_worker_budget installs the transport; the provider it builds must
    carry our document, and the event log must say so."""

    def setUp(self):
        self._previous = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS

    def tearDown(self):
        _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = self._previous

    def _invocation(self, task_id: str, **kwargs):
        return SimpleNamespace(
            provider_id="claude",
            prompt="ENVELOPE: Task contract source ...",
            model_name=None,
            task_lock=SimpleNamespace(task_id=task_id),
            kwargs=kwargs,
            input_artifacts=(),
        )

    def test_our_task_gets_the_document(self):
        from shepherd_dev.events import RunEventLog, WorkerStreamHook, load_run_events
        from shepherd_dev.supervisor import set_worker_budget

        root = Path(tempfile.mkdtemp(prefix="shepherd-prompt-"))
        log = RunEventLog(run_id="r1", root=root)
        hook = WorkerStreamHook(log)
        self.assertTrue(set_worker_budget(300, stream_hook=hook))
        provider = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude(
            self._invocation(
                "shepherd_dev.tasks.implement",
                repo=object(), feature=FEATURE, context=PACK, guidance="", gate="mix test",
            )
        )
        self.assertTrue(provider.prompt.startswith("Implement the requested feature"))
        self.assertNotIn("ENVELOPE", provider.prompt)
        self.assertIn("## context", provider.prompt)
        self.assertEqual(provider.budget_seconds, 300)
        kinds = [e["kind"] for e in load_run_events("r1", root=root)]
        self.assertIn("worker.prompt", kinds)
        event = next(e for e in load_run_events("r1", root=root) if e["kind"] == "worker.prompt")
        self.assertTrue(event["payload"]["rendered"])
        self.assertEqual(event["payload"]["sections"], ["context", "feature", "gate"])

    def test_a_foreign_task_keeps_the_envelope(self):
        from shepherd_dev.supervisor import set_worker_budget

        set_worker_budget(300)
        provider = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude(
            self._invocation("shepherd_dev.tasks.smoke_change", output_path="x", output_text="y")
        )
        self.assertEqual(provider.prompt, "ENVELOPE: Task contract source ...")

    def test_a_malformed_invocation_never_blocks_the_launch(self):
        from shepherd_dev.supervisor import set_worker_budget

        set_worker_budget(300)
        provider = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude(
            SimpleNamespace(provider_id="claude", prompt="ENVELOPE", model_name=None)
        )
        self.assertEqual(provider.prompt, "ENVELOPE")


class DevelopPassesTheInputsApart(unittest.TestCase):
    """develop() used to hand the worker `guidance = pack + guidance`. Now the
    pack, the feedback and the gate command travel under their own names."""

    def _run(self, *, context_pack, test_cmd):
        from shepherd_dev import supervisor as sup

        seen: list[dict] = []

        class _Output:
            def changeset(self):
                return {"file.py": b"v1\n"}

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
                seen.append(dict(kw))
                return _Run()

        orig = (sup.read_changeset_entries, sup._run_gate, sup._start_gate_warmup)
        sup.read_changeset_entries = lambda cs: dict(cs)
        sup._run_gate = lambda *a, **k: sup.GateResult(False, 1, "boom")
        sup._start_gate_warmup = lambda *a, **k: None
        try:
            sup.develop(
                _Workspace(), object(), repo="R", repo_root=Path("/r"), feature="add X",
                test_cmd=test_cmd, max_attempts=2, context_pack=context_pack,
            )
        finally:
            sup.read_changeset_entries, sup._run_gate, sup._start_gate_warmup = orig
        return seen

    def test_first_attempt_has_the_pack_under_context_and_no_guidance(self):
        seen = self._run(context_pack=PACK, test_cmd="pytest -q")
        self.assertEqual(seen[0]["context"], PACK)
        self.assertEqual(seen[0]["guidance"], "")
        self.assertEqual(seen[0]["gate"], "pytest -q")
        self.assertEqual(seen[0]["feature"], "add X")

    def test_the_retry_keeps_the_pack_and_adds_real_guidance(self):
        seen = self._run(context_pack=PACK, test_cmd="pytest -q")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1]["context"], PACK)
        self.assertIn("PREVIOUS ATTEMPT", seen[1]["guidance"])
        self.assertNotIn(PACK, seen[1]["guidance"])

    def test_no_gate_means_an_empty_gate_input(self):
        seen = self._run(context_pack=None, test_cmd=None)
        self.assertEqual(seen[0]["gate"], "")
        self.assertEqual(seen[0]["context"], "")


class GateNoteTests(unittest.TestCase):
    def test_no_gate_no_note(self):
        from shepherd_dev.supervisor import _gate_note

        self.assertEqual(_gate_note(None, None), "")

    def test_pending_verdict_says_so(self):
        from shepherd_dev.supervisor import _gate_note

        self.assertIn("not in yet", _gate_note("mix test", None))

    def test_a_passed_gate_reads_passed_with_its_tail(self):
        from shepherd_dev.supervisor import GateResult, _gate_note

        note = _gate_note("mix test", GateResult(True, 0, "a\nb\n952 tests, 0 failures\n"))
        self.assertTrue(note.startswith("mix test\nPASSED (exit 0)"))
        self.assertIn("952 tests, 0 failures", note)

    def test_a_failed_gate_reads_failed(self):
        from shepherd_dev.supervisor import GateResult, _gate_note

        self.assertIn("FAILED (exit 2)", _gate_note("mix test", GateResult(False, 2, "1 failure")))

    def test_an_infra_error_is_named(self):
        from shepherd_dev.supervisor import GateResult, _gate_note

        note = _gate_note("mix test", GateResult(False, None, "", infra_error="no mix"))
        self.assertIn("could not run: no mix", note)


if __name__ == "__main__":
    unittest.main()
