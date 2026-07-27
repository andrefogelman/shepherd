"""Tests for gate-stage teardown under an open workspace.

The staged copy symlinks the repo's dependency dirs back into the real tree.
Tearing that stage down in-process asks the substrate's os.unlink patch to
resolve the link, which lands inside the OPEN workspace and is refused as an
unscoped mutation — an error that is not an OSError, so ignore_errors=True does
not swallow it and the run dies mid-gate. Teardown therefore happens in a child
interpreter, which carries none of the patches.

The unit tests below simulate the guard (deterministic, no substrate needed);
the integration test at the bottom exercises the real one when a `shepherd`
binary is available to create a throwaway workspace.
Runnable with: python -m unittest tests.test_gate_teardown
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdirs import mkdtemp  # noqa: E402

from shepherd_dev.supervisor import DEP_DIRS, LocalGateStage, _remove_tree  # noqa: E402


class UnscopedMutation(Exception):
    """Stand-in for the substrate's refusal: deliberately NOT an OSError, which
    is the property that makes ignore_errors=True useless against it."""


def _fd_directory(dir_fd: int) -> Path | None:
    """The directory a dir_fd points at, resolved the way the substrate does:
    F_GETPATH where it exists (macOS), the /proc or /dev fd links elsewhere."""
    try:
        import fcntl

        raw = fcntl.fcntl(dir_fd, fcntl.F_GETPATH, b"\0" * 1024)  # type: ignore[attr-defined]
        return Path(os.fsdecode(raw.split(b"\0", 1)[0]))
    except Exception:
        pass
    for root in ("/proc/self/fd", "/dev/fd"):
        try:
            return Path(os.readlink(f"{root}/{dir_fd}"))
        except OSError:
            continue
    return None


class _GuardedUnlink:
    """os.unlink patched the way the substrate patches it: a candidate whose
    resolved target lands inside the guarded tree is refused."""

    def __init__(self, guarded: Path):
        self.guarded = guarded.resolve()
        self._real = os.unlink

    def __enter__(self):
        os.unlink = self  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        os.unlink = self._real  # type: ignore[assignment]

    def __call__(self, path, *, dir_fd=None):
        raw = Path(os.fspath(path))
        if raw.is_absolute():
            resolved = raw.resolve()
        elif dir_fd is None:
            resolved = (Path.cwd() / raw).resolve()
        else:
            base = _fd_directory(dir_fd)
            if base is None:  # unresolvable fd: the substrate refuses too, but
                return self._real(path, dir_fd=dir_fd)  # that is a different defect
            resolved = (base / raw).resolve()
        if resolved == self.guarded or self.guarded in resolved.parents:
            raise UnscopedMutation(
                f"Workspace mutation 'os.unlink' for {raw} requires an active scope"
            )
        return self._real(path, dir_fd=dir_fd)


def _link_deps(guarded: Path, dest: Path, names=DEP_DIRS) -> None:
    """The link shape _link_dep_dirs produces, for the named dep dirs."""
    for name in names:
        (guarded / name).mkdir(parents=True, exist_ok=True)
        (dest / name).symlink_to((guarded / name).resolve())


def _stage_with_link_into(guarded: Path, names=DEP_DIRS) -> Path:
    """A stage shaped like the gate's: real files plus dep-dir symlinks."""
    root = Path(mkdtemp(prefix="shepherd-teardown-test-"))
    (root / "base" / "src").mkdir(parents=True)
    (root / "base" / "src" / "mod.py").write_bytes(b"x = 1\n")
    _link_deps(guarded, root / "base", names)
    return root


class GuardSimulationTests(unittest.TestCase):
    """The simulated guard must actually reproduce the failure, or the tests
    below prove nothing."""

    def test_the_fd_directory_probe_resolves_on_this_platform(self):
        """Pinned separately so a platform where the probe cannot resolve a
        dir_fd fails here by name, instead of quietly turning every test below
        into one that raises nothing and proves nothing."""
        directory = Path(mkdtemp(prefix="shepherd-fdprobe-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        fd = os.open(directory, os.O_RDONLY)
        self.addCleanup(os.close, fd)

        resolved = _fd_directory(fd)
        self.assertIsNotNone(resolved, "no fd-to-path mechanism works here")
        assert resolved is not None  # narrowing, for type checkers
        self.assertEqual(resolved.resolve(), directory.resolve())

    def test_an_in_process_rmtree_dies_on_a_link_into_the_guarded_tree(self):
        for name in DEP_DIRS:
            with self.subTest(dep_dir=name):
                guarded = Path(mkdtemp(prefix="shepherd-guarded-"))
                self.addCleanup(shutil.rmtree, guarded, ignore_errors=True)
                stage = _stage_with_link_into(guarded, [name])
                self.addCleanup(shutil.rmtree, stage, ignore_errors=True)

                with _GuardedUnlink(guarded):
                    # ignore_errors=True and it still escapes: the whole defect.
                    with self.assertRaises(UnscopedMutation):
                        shutil.rmtree(stage, ignore_errors=True)
                self.assertTrue(stage.exists())

    def test_the_guard_leaves_an_ordinary_stage_alone(self):
        guarded = Path(mkdtemp(prefix="shepherd-guarded-"))
        self.addCleanup(shutil.rmtree, guarded, ignore_errors=True)
        stage = Path(mkdtemp(prefix="shepherd-teardown-test-"))
        (stage / "src").mkdir()
        (stage / "src" / "mod.py").write_bytes(b"x = 1\n")

        with _GuardedUnlink(guarded):
            shutil.rmtree(stage, ignore_errors=True)
        self.assertFalse(stage.exists())


class RemoveTreeTests(unittest.TestCase):
    def test_teardown_survives_a_link_into_the_guarded_tree(self):
        guarded = Path(mkdtemp(prefix="shepherd-guarded-"))
        self.addCleanup(shutil.rmtree, guarded, ignore_errors=True)
        stage = _stage_with_link_into(guarded)
        self.addCleanup(shutil.rmtree, stage, ignore_errors=True)

        with _GuardedUnlink(guarded):
            _remove_tree(stage)  # in-process rmtree would raise here
        self.assertFalse(stage.exists())
        # The links were followed by nobody: the real dep dirs are untouched.
        for name in DEP_DIRS:
            self.assertTrue((guarded / name).is_dir(), name)

    def test_local_gate_stage_close_survives_the_same_stage(self):
        guarded = Path(mkdtemp(prefix="shepherd-guarded-"))
        self.addCleanup(shutil.rmtree, guarded, ignore_errors=True)
        stage = LocalGateStage(guarded)
        self.addCleanup(shutil.rmtree, stage._root, ignore_errors=True)
        (stage._root / "base").mkdir()
        (stage._root / "base" / "mod.py").write_bytes(b"x = 1\n")
        _link_deps(guarded, stage._root / "base")

        with _GuardedUnlink(guarded):
            stage.close()
        self.assertFalse(stage._root.exists())

    def test_teardown_of_a_plain_tree_still_removes_it(self):
        stage = Path(mkdtemp(prefix="shepherd-teardown-test-"))
        (stage / "a").mkdir()
        (stage / "a" / "b.py").write_bytes(b"1\n")
        _remove_tree(stage)
        self.assertFalse(stage.exists())

    def test_teardown_of_a_missing_tree_is_a_no_op(self):
        missing = Path(mkdtemp(prefix="shepherd-teardown-test-")) / "gone"
        _remove_tree(missing)  # must not raise
        self.assertFalse(missing.exists())


def _shepherd_cli() -> str | None:
    """The `shepherd` entry point, preferring the one in this interpreter's own
    environment — the tool venv exposes only `shepherd-dev` on PATH."""
    local = Path(sys.executable).parent / "shepherd"
    return str(local) if local.exists() else shutil.which("shepherd")


@unittest.skipUnless(_shepherd_cli(), "shepherd CLI not available")
class RealSubstrateTeardownTests(unittest.TestCase):
    """The same teardown against the real substrate, in a throwaway workspace.

    Everything else in this suite drives shepherd with the substrate stubbed or
    simulated; this is the one place the actual patch is loaded.
    """

    def _workspace(self) -> Path:
        repo = Path(mkdtemp(prefix="shepherd-ws-test-")) / "repo"
        repo.mkdir()
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        for cmd in (
            ["git", "init", "-q", "."],
            ["git", "config", "user.email", "test@example.invalid"],
            ["git", "config", "user.name", "test"],
        ):
            subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
        (repo / "file.txt").write_bytes(b"x\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
        cli = _shepherd_cli()
        assert cli is not None  # the class is skipped otherwise
        done = subprocess.run([cli, "init"], cwd=repo, capture_output=True, timeout=180)
        if done.returncode != 0 or not (repo / ".vcscore").is_dir():
            self.skipTest("shepherd init did not produce a workspace")
        return repo

    def test_teardown_survives_a_dep_link_with_the_workspace_open(self):
        import shepherd as sp

        repo = self._workspace()
        stage = LocalGateStage(repo)
        self.addCleanup(shutil.rmtree, stage._root, ignore_errors=True)
        (stage._root / "base").mkdir()
        (stage._root / "base" / "mod.py").write_bytes(b"x = 1\n")
        _link_deps(repo, stage._root / "base")  # every dep dir, as the gate links them

        cwd = Path.cwd()
        os.chdir(repo)  # the gate tears down with cwd inside the repo
        try:
            with sp.open(repo):
                stage.close()
        finally:
            os.chdir(cwd)
        self.assertFalse(stage._root.exists())
        for name in DEP_DIRS:
            self.assertTrue((repo / name).is_dir(), name)


if __name__ == "__main__":
    unittest.main()
