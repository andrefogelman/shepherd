"""`jail_env`: repo-declared environment the worker needs to check its own work.

The worker's jail is materialized from a git tree, so everything gitignored —
deps/, _build/, node_modules/, target/, .venv/ — is absent by construction. A
compiled language's worker therefore cannot compile, and a class of error that
`mix compile` reports in seconds costs a full 13-minute attempt plus a gate
instead. Measured on a real Phoenix repo: a git-archive checkout (13M, no
deps/, no _build/) compiles in 42s when MIX_DEPS_PATH points at a cache
outside it, and the cache is only read — zero files touched.

The substrate offers no per-run env hook (RuntimeOptions carries trace,
provider and model, nothing else), so the values travel through the process
environment the provider's child inherits.

Runnable with: python -m unittest tests.test_jail_env
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class _Repo:
    """A repo root carrying a .shepherd-dev.json."""

    def __init__(self, config):
        from tmpdirs import mkdtemp

        self.root = Path(mkdtemp(prefix="shepherd-jailenv-"))
        if config is not None:
            (self.root / ".shepherd-dev.json").write_text(
                json.dumps(config), encoding="utf-8"
            )


class JailEnvConfigTests(unittest.TestCase):
    def test_absent_config_is_an_empty_mapping_not_an_error(self):
        from shepherd_dev.config import jail_env

        self.assertEqual(jail_env(_Repo(None).root), {})
        self.assertEqual(jail_env(_Repo({"test_cmd": "mix test"}).root), {})

    def test_declared_values_come_back_verbatim(self):
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {"MIX_BUILD_ROOT": ".claude-scratch/build"}})
        self.assertEqual(jail_env(repo.root), {"MIX_BUILD_ROOT": ".claude-scratch/build"})

    def test_a_leading_tilde_is_expanded_because_the_child_shell_will_not(self):
        """The value reaches the worker as an environment variable, not as
        shell input — nothing expands `~` on the way."""
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "~/.cache/sac-deps"}})
        got = jail_env(repo.root)["MIX_DEPS_PATH"]
        self.assertTrue(got.startswith(str(Path.home())), got)
        self.assertNotIn("~", got)

    def test_a_relative_path_stays_relative(self):
        """MIX_BUILD_ROOT=.claude-scratch/build means "inside the clone", and
        the clone is the worker's cwd. Resolving it here would bind it to
        whatever directory the supervisor happened to run from."""
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {"MIX_BUILD_ROOT": ".claude-scratch/build"}})
        self.assertEqual(jail_env(repo.root)["MIX_BUILD_ROOT"], ".claude-scratch/build")

    def test_non_string_entries_are_dropped_rather_than_coerced(self):
        from shepherd_dev.config import jail_env

        repo = _Repo({"jail_env": {"GOOD": "1", "BAD": 2, "ALSO_BAD": None}})
        self.assertEqual(jail_env(repo.root), {"GOOD": "1"})

    def test_a_non_object_jail_env_is_ignored(self):
        from shepherd_dev.config import jail_env

        for bad in ("MIX_DEPS_PATH=x", ["MIX_DEPS_PATH"], 3):
            with self.subTest(value=bad):
                self.assertEqual(jail_env(_Repo({"jail_env": bad}).root), {})

    def test_variables_that_would_redirect_our_own_interpreter_are_refused(self):
        """CHILD_ENV_STRIP exists because a wrong PYTHONHOME stops the
        interpreter booting before any of our code runs. A repo config must
        not be able to reintroduce exactly what we scrub."""
        from shepherd_dev.config import jail_env

        repo = _Repo({
            "jail_env": {
                "PYTHONPATH": "/evil",
                "PYTHONHOME": "/evil",
                "VIRTUAL_ENV": "/evil",
                "MIX_DEPS_PATH": "/ok",
            }
        })
        self.assertEqual(jail_env(repo.root), {"MIX_DEPS_PATH": "/ok"})


class JailEnvApplicationTests(unittest.TestCase):
    def setUp(self):
        self._before = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._before)))

    def test_the_values_are_visible_inside_the_block(self):
        from shepherd_dev.config import jail_env_applied

        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "/cache/deps"}})
        self.assertNotIn("MIX_DEPS_PATH", os.environ)
        with jail_env_applied(repo.root):
            self.assertEqual(os.environ["MIX_DEPS_PATH"], "/cache/deps")

    def test_the_environment_is_restored_afterwards(self):
        from shepherd_dev.config import jail_env_applied

        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "/cache/deps"}})
        with jail_env_applied(repo.root):
            pass
        self.assertNotIn("MIX_DEPS_PATH", os.environ)

    def test_a_pre_existing_value_is_restored_not_deleted(self):
        from shepherd_dev.config import jail_env_applied

        os.environ["MIX_DEPS_PATH"] = "/mine"
        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "/cache/deps"}})
        with jail_env_applied(repo.root):
            self.assertEqual(os.environ["MIX_DEPS_PATH"], "/cache/deps")
        self.assertEqual(os.environ["MIX_DEPS_PATH"], "/mine")

    def test_the_environment_is_restored_even_when_the_block_raises(self):
        from shepherd_dev.config import jail_env_applied

        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "/cache/deps"}})
        with self.assertRaises(RuntimeError):
            with jail_env_applied(repo.root):
                raise RuntimeError("boom")
        self.assertNotIn("MIX_DEPS_PATH", os.environ)

    def test_an_empty_config_is_a_no_op_block(self):
        from shepherd_dev.config import jail_env_applied

        before = dict(os.environ)
        with jail_env_applied(_Repo(None).root):
            self.assertEqual(dict(os.environ), before)


class CommandsApplyItTests(unittest.TestCase):
    """Wiring, not plumbing: the value has to be in the environment while the
    command runs, because that is the only thing the provider's child
    inherits. A helper nobody calls would pass every test above."""

    def setUp(self):
        self._before = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._before)))

    def _seen_by(self, command: str) -> str | None:
        import shepherd_dev.cli as C

        repo = _Repo({"jail_env": {"MIX_DEPS_PATH": "/cache/deps"}})
        (repo.root / ".vcscore").mkdir()  # _resolve_repo's workspace marker
        seen = {}

        def _inner(args, repo_root):
            seen["value"] = os.environ.get("MIX_DEPS_PATH")
            return 0

        attr = {"run": "_cmd_run_inner", "run2": "_cmd_run2_inner"}[command]
        real = getattr(C, attr)
        real_opt = C._maybe_optimize_after
        setattr(C, attr, _inner)
        C._maybe_optimize_after = lambda *a, **k: None
        try:
            args = type("A", (), {"repo": str(repo.root)})()
            {"run": C.cmd_run, "run2": C.cmd_run2}[command](args)
        finally:
            setattr(C, attr, real)
            C._maybe_optimize_after = real_opt
        return seen.get("value")

    def test_run_applies_it(self):
        self.assertEqual(self._seen_by("run"), "/cache/deps")

    def test_run2_applies_it(self):
        self.assertEqual(self._seen_by("run2"), "/cache/deps")

    def test_it_does_not_outlive_the_command(self):
        self._seen_by("run")
        self.assertNotIn("MIX_DEPS_PATH", os.environ)


if __name__ == "__main__":
    unittest.main()
