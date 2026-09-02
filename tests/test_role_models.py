"""Which model serves which role, and at what effort, is decided — not left
to whatever the CLI defaults to that day.

Runnable with: python -m unittest tests.test_role_models
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.launch import RoleModel, RoleModels, model_argv  # noqa: E402


def _claude_argv() -> list[str]:
    return ["/usr/bin/env", "HOME=/w", "/usr/local/bin/claude", "-p", "x",
            "--permission-mode", "bypassPermissions", "--tools", "default"]


class Resolution(unittest.TestCase):
    def test_nothing_configured_means_nothing_chosen(self):
        roles = RoleModels.from_config(None, config={}, global_config={})
        self.assertTrue(roles.worker.empty)
        self.assertTrue(roles.reviewer.empty)
        self.assertEqual(roles.describe(), {})

    def test_repo_config_over_global_over_nothing(self):
        roles = RoleModels.from_config(
            None,
            global_config={"models": {"worker": {"model": "g-worker", "effort": "low"}, "reviewer": "g-review"}},
            config={"models": {"worker": {"model": "r-worker"}}},
        )
        self.assertEqual(roles.worker.model, "r-worker")
        self.assertEqual(roles.worker.effort, "low")  # kept from the global layer
        self.assertEqual(roles.reviewer.model, "g-review")  # a bare string is a model name

    def test_flags_over_everything(self):
        roles = RoleModels.from_config(
            None, config={"models": {"worker": {"model": "r", "effort": "low"}}}, global_config={},
            worker_model="flag-worker", reviewer_model="flag-review", effort="max",
        )
        self.assertEqual(roles.worker.model, "flag-worker")
        self.assertEqual(roles.worker.effort, "max")
        self.assertEqual(roles.reviewer.model, "flag-review")
        self.assertEqual(roles.reviewer.effort, "max")

    def test_an_unknown_effort_is_dropped_with_a_warning_not_sent_to_the_cli(self):
        roles = RoleModels.from_config(None, config={"models": {"worker": {"effort": "extreme"}}}, global_config={})
        self.assertIsNone(roles.worker.effort)
        self.assertEqual(len(roles.warnings), 1)
        self.assertIn("extreme", roles.warnings[0])

    def test_the_task_id_names_the_role(self):
        roles = RoleModels(worker=RoleModel(model="w"), reviewer=RoleModel(model="r"))
        self.assertEqual(roles.for_task("shepherd_dev.tasks.implement").model, "w")
        self.assertEqual(roles.for_task("shepherd_dev.tasks.write_tests").model, "w")
        self.assertEqual(roles.for_task("shepherd_dev.tasks.review").model, "r")
        self.assertTrue(roles.for_task("shepherd_dev.tasks.smoke_change").empty)
        self.assertTrue(roles.for_task("").empty)

    def test_it_reads_the_repo_config_file(self):
        import tempfile

        from shepherd_dev import config

        repo = Path(tempfile.mkdtemp(prefix="shepherd-roles-"))
        config.save_config(repo, {"models": {"reviewer": {"model": "file-review", "fallback": "fb"}}})
        roles = RoleModels.from_config(repo, global_config={})
        self.assertEqual(roles.reviewer.model, "file-review")
        self.assertEqual(roles.reviewer.fallback, "fb")


class PlannerAlias(unittest.TestCase):
    def test_models_planner_reaches_planning_config(self):
        import os
        import tempfile
        from unittest.mock import patch

        from shepherd_dev import config

        repo = Path(tempfile.mkdtemp(prefix="shepherd-roles-"))
        config.save_config(repo, {"models": {"planner": "planner-model"}})
        with patch.dict(os.environ, {"SHEPHERD_DEV_CONFIG": str(repo / "no-global.json")}):
            with patch.object(config, "GLOBAL_CONFIG", repo / "no-global.json"):
                self.assertEqual(config.planning_config(repo)["model"], "planner-model")
                # the specific key still wins within the same file
                config.save_config(repo, {"planning": {"model": "specific"}})
                self.assertEqual(config.planning_config(repo)["model"], "specific")


class ArgvShape(unittest.TestCase):
    def test_effort_and_fallback_are_appended_model_is_not(self):
        argv = model_argv(_claude_argv(), RoleModel(model="m", effort="high", fallback="fb"))
        self.assertEqual(argv[-4:], ["--effort", "high", "--fallback-model", "fb"])
        self.assertNotIn("--model", argv)  # the provider's own field emits it

    def test_empty_role_changes_nothing(self):
        base = _claude_argv()
        self.assertEqual(model_argv(base, RoleModel()), base)

    def test_a_non_claude_argv_is_untouched(self):
        self.assertEqual(model_argv(["/bin/sh", "-c", "x"], RoleModel(effort="high")), ["/bin/sh", "-c", "x"])

    def test_never_twice(self):
        once = model_argv(_claude_argv(), RoleModel(effort="high"))
        self.assertEqual(model_argv(once, RoleModel(effort="high")), once)


try:
    from shepherd_dialect.workspace_control import runtime_provider as _rp

    _HAS_SUBSTRATE = True
except Exception:  # pragma: no cover - substrate absent
    _HAS_SUBSTRATE = False


@unittest.skipUnless(_HAS_SUBSTRATE, "shepherd substrate not installed")
class ThroughTheTransport(unittest.TestCase):
    def setUp(self):
        self._previous = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS

    def tearDown(self):
        _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = self._previous

    def _invocation(self, task_id, model_name=None):
        return SimpleNamespace(
            provider_id="claude", prompt="p", model_name=model_name,
            task_lock=SimpleNamespace(task_id=task_id), kwargs={},
        )

    def test_the_worker_and_the_reviewer_get_their_own_model_and_effort(self):
        from shepherd_dev.supervisor import set_worker_budget

        roles = RoleModels(
            worker=RoleModel(model="w-model", effort="high"),
            reviewer=RoleModel(model="r-model", effort="max", fallback="r-fallback"),
        )
        self.assertTrue(set_worker_budget(300, roles=roles))
        transport = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude

        worker = transport(self._invocation("shepherd_dev.tasks.implement"))
        argv = [str(a) for a in worker.command_argv("/w", "/usr/local/bin/claude")]
        self.assertEqual(worker.model, "w-model")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "w-model")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertNotIn("--fallback-model", argv)

        reviewer = transport(self._invocation("shepherd_dev.tasks.review"))
        argv = [str(a) for a in reviewer.command_argv("/w", "/usr/local/bin/claude")]
        self.assertEqual(argv[argv.index("--model") + 1], "r-model")
        self.assertEqual(argv[argv.index("--effort") + 1], "max")
        self.assertEqual(argv[argv.index("--fallback-model") + 1], "r-fallback")

    def test_a_model_named_on_the_run_outranks_the_role(self):
        from shepherd_dev.supervisor import set_worker_budget

        set_worker_budget(300, roles=RoleModels(worker=RoleModel(model="w-model")))
        provider = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude(
            self._invocation("shepherd_dev.tasks.implement", model_name="explicit")
        )
        self.assertEqual(provider.model, "explicit")

    def test_no_roles_means_the_cli_default_as_before(self):
        from shepherd_dev.supervisor import set_worker_budget

        set_worker_budget(300)
        provider = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude(self._invocation("shepherd_dev.tasks.implement"))
        argv = [str(a) for a in provider.command_argv("/w", "/usr/local/bin/claude")]
        self.assertIsNone(provider.model)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)


class CliFlags(unittest.TestCase):
    def test_run_run2_and_runN_accept_the_flags(self):
        from shepherd_dev.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "f", "--model", "m", "--reviewer-model", "r", "--effort", "high"])
        self.assertEqual((args.model, args.reviewer_model, args.effort), ("m", "r", "high"))
        args = parser.parse_args(["run2", "a", "b", "--effort", "max"])
        self.assertEqual(args.effort, "max")
        args = parser.parse_args(["runN", "a", "b", "--model", "m"])
        self.assertEqual(args.model, "m")
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "f", "--effort", "extreme"])


if __name__ == "__main__":
    unittest.main()
