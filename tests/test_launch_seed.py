"""Each launch gets its own writable copy of the repo's toolchain caches.

The worker's clone is a git tree: deps/, _build/, node_modules/ are absent by
construction, and the jail denies writes anywhere else. Workers that could
not compile burned turns discovering it and then shipped the repository to a
remote host to compile there. Now a jail_seed origin is cloned per launch,
named by its variable for that launch only, and added to the launch's
writable roots; a jail_seed_links origin is cloned and linked into the tree.

Runnable with: python -m unittest tests.test_launch_seed
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.seed import LaunchSeed, link_paths, seed_argv, widen_confinement  # noqa: E402


def _origin(name: str, files: int = 3) -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"shepherd-seed-origin-{name}-"))
    for i in range(files):
        (root / f"{name}_{i}.beam").write_bytes(b"x" * 10)
    return root


class Config(unittest.TestCase):
    def test_links_must_stay_inside_the_tree(self):
        from shepherd_dev import config

        repo = Path(tempfile.mkdtemp(prefix="shepherd-seed-cfg-"))
        config.save_config(repo, {"jail_seed_links": {
            "node_modules": "~/cache/nm", "/abs": "x", "../up": "y", "deep/dir/": "z", "": "w",
        }})
        links = config.jail_seed_links(repo)
        self.assertEqual(set(links), {"node_modules", "deep/dir"})
        self.assertTrue(links["node_modules"].startswith(str(Path.home())))

    def test_from_config_reads_both_blocks(self):
        from shepherd_dev import config

        repo = Path(tempfile.mkdtemp(prefix="shepherd-seed-cfg-"))
        config.save_config(repo, {"jail_seed": {"MIX_BUILD_PATH": "/warm/_build"}, "jail_seed_links": {"node_modules": "/warm/nm"}})
        seed = LaunchSeed.from_config(repo)
        self.assertEqual(seed.env_origins, {"MIX_BUILD_PATH": "/warm/_build"})
        self.assertEqual(seed.link_origins, {"node_modules": "/warm/nm"})
        self.assertFalse(seed.empty)
        self.assertTrue(LaunchSeed.from_config(Path(tempfile.mkdtemp())).empty)
        self.assertEqual(link_paths(seed), ("node_modules",))


class Prepare(unittest.TestCase):
    def test_each_origin_is_copied_named_linked_and_writable_then_taken_back(self):
        build = _origin("build")
        nm = _origin("nm")
        work = Path(tempfile.mkdtemp(prefix="shepherd-seed-work-"))
        seed = LaunchSeed(env_origins={"MIX_BUILD_PATH": str(build)}, link_origins={"node_modules": str(nm)})
        plan = seed.prepare(work)
        # the env copy is a real, separate, writable directory holding the origin's files
        env_dir = Path(plan.env["MIX_BUILD_PATH"])
        self.assertTrue(env_dir.is_dir())
        self.assertNotEqual(env_dir.resolve(), build.resolve())
        self.assertEqual(sorted(p.name for p in env_dir.iterdir()), sorted(p.name for p in build.iterdir()))
        (env_dir / "new.beam").write_bytes(b"compiled")  # writable, and the origin untouched
        self.assertFalse((build / "new.beam").exists())
        # the link sits in the tree and resolves to its own copy
        link = work / "node_modules"
        self.assertTrue(link.is_symlink())
        self.assertNotEqual(link.resolve(), nm.resolve())
        self.assertEqual(sorted(p.name for p in link.iterdir()), sorted(p.name for p in nm.iterdir()))
        self.assertEqual(set(plan.roots), {str(env_dir), str(plan.links[0][1])})
        self.assertEqual(plan.describe(), {"env": ["MIX_BUILD_PATH"], "links": ["node_modules"], "writable_roots": 2})
        plan.cleanup(work)
        self.assertFalse(link.exists())
        self.assertFalse(link.is_symlink())
        self.assertFalse(env_dir.exists())

    def test_a_missing_origin_yields_an_empty_writable_directory(self):
        work = Path(tempfile.mkdtemp(prefix="shepherd-seed-work-"))
        plan = LaunchSeed(env_origins={"CARGO_TARGET_DIR": "/nonexistent/origin"}).prepare(work)
        target = Path(plan.env["CARGO_TARGET_DIR"])
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        plan.cleanup(work)

    def test_two_launches_never_share_a_copy(self):
        build = _origin("build")
        work = Path(tempfile.mkdtemp(prefix="shepherd-seed-work-"))
        seed = LaunchSeed(env_origins={"MIX_BUILD_PATH": str(build)})
        a = seed.prepare(work)
        b = seed.prepare(work)
        self.assertNotEqual(a.env["MIX_BUILD_PATH"], b.env["MIX_BUILD_PATH"])
        a.cleanup(work)
        b.cleanup(work)

    def test_an_empty_seed_prepares_nothing(self):
        plan = LaunchSeed().prepare(Path(tempfile.mkdtemp()))
        self.assertEqual((plan.env, plan.links, plan.roots), ({}, [], ()))
        plan.cleanup(None)


class ArgvAndConfinement(unittest.TestCase):
    def test_variables_go_right_after_the_env_binary_for_this_launch_only(self):
        argv = ["/usr/bin/perl", "-e", "alarm", "900", "/usr/bin/env", "HOME=/w", "/usr/local/bin/claude", "-p", "x"]
        out = seed_argv(argv, {"MIX_BUILD_PATH": "/tmp/s/env/MIX_BUILD_PATH", "A": "1"})
        i = out.index("/usr/bin/env")
        self.assertEqual(out[i + 1:i + 4], ["A=1", "MIX_BUILD_PATH=/tmp/s/env/MIX_BUILD_PATH", "HOME=/w"])
        self.assertEqual(seed_argv(argv, {}), argv)
        self.assertEqual(seed_argv(["/bin/sh", "-c", "x"], {"A": "1"}), ["/bin/sh", "-c", "x"])

    def test_writable_roots_are_widened_and_a_read_only_spec_stays_read_only(self):
        @dataclasses.dataclass(frozen=True)
        class Spec:
            writable_roots: tuple = ()
            network: str = "allow"

        widened = widen_confinement(Spec(writable_roots=("/w",)), ("/tmp/s/env/X",))
        self.assertEqual(widened.writable_roots, ("/w", "/tmp/s/env/X"))
        self.assertEqual(widened.network, "allow")
        self.assertEqual(widen_confinement(Spec(), ("/tmp/s/env/X",)).writable_roots, ())
        self.assertEqual(widen_confinement("not a spec", ("/x",)), "not a spec")
        self.assertEqual(widen_confinement(Spec(writable_roots=("/w",)), ()).writable_roots, ("/w",))


class ThroughThePreparingProxy(unittest.TestCase):
    def test_the_launch_sees_its_copies_and_they_are_gone_afterwards(self):
        from shepherd_dev.supervisor import _PreparingExecution

        build = _origin("build")
        nm = _origin("nm")
        work = Path(tempfile.mkdtemp(prefix="shepherd-seed-work-"))
        seen: dict = {}

        @dataclasses.dataclass(frozen=True)
        class Spec:
            writable_roots: tuple = ()

        class _Inner:
            working_path = work

            def launch_confined(self, command, confinement):
                seen["command"] = list(command)
                seen["roots"] = confinement.writable_roots
                seen["link_ok"] = (work / "node_modules").is_symlink()
                seen["env_dir"] = next(a for a in command if str(a).startswith("MIX_BUILD_PATH=")).split("=", 1)[1]
                seen["env_dir_exists"] = Path(seen["env_dir"]).is_dir()
                return "launched"

        events: list = []
        hook = SimpleNamespace(emit=lambda kind, payload: events.append((kind, payload)))
        seed = LaunchSeed(env_origins={"MIX_BUILD_PATH": str(build)}, link_origins={"node_modules": str(nm)})
        proxy = _PreparingExecution(_Inner(), seed=seed, hook=hook)
        argv = ["/usr/bin/env", "HOME=/w", "/usr/local/bin/claude", "-p", "x"]
        self.assertEqual(proxy.launch_confined(argv, Spec(writable_roots=(str(work),))), "launched")
        self.assertTrue(seen["link_ok"])
        self.assertTrue(seen["env_dir_exists"])
        self.assertEqual(len(seen["roots"]), 3)
        self.assertEqual(seen["roots"][0], str(work))
        self.assertIn(seen["env_dir"], seen["roots"])
        self.assertEqual(events[0][0], "worker.seed")
        # taken back: no link, no copies
        self.assertFalse((work / "node_modules").is_symlink())
        self.assertFalse(Path(seen["env_dir"]).exists())

    def test_an_empty_seed_is_a_plain_launch(self):
        from shepherd_dev.supervisor import _PreparingExecution

        class _Inner:
            working_path = Path("/w")

            def launch_confined(self, command, confinement):
                return (list(command), confinement)

        out = _PreparingExecution(_Inner(), seed=LaunchSeed()).launch_confined(["a"], "spec")
        self.assertEqual(out, (["a"], "spec"))


class TheChangesetForgetsTheLinks(unittest.TestCase):
    def test_link_paths_never_enter_the_entries(self):
        from shepherd_dev.supervisor import read_changeset_entries

        class _CS:
            changed_paths = ["node_modules", "node_modules/pkg/index.js", "lib/a.ex", "node_modules_extra/x"]

            def read_file(self, rel):
                return (b"c", 0o100644)

        entries = read_changeset_entries(_CS(), ignore=("node_modules",))
        self.assertEqual(sorted(entries), ["lib/a.ex", "node_modules_extra/x"])


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

    def test_the_seed_rides_on_the_provider(self):
        from shepherd_dev.supervisor import set_worker_budget

        seed = LaunchSeed(env_origins={"MIX_BUILD_PATH": "/warm"})
        set_worker_budget(300, seed=seed)
        provider = _rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude(SimpleNamespace(
            provider_id="claude", prompt="p", model_name=None,
            task_lock=SimpleNamespace(task_id="shepherd_dev.tasks.implement"), kwargs={},
        ))
        self.assertIs(provider._seed, seed)


if __name__ == "__main__":
    unittest.main()
