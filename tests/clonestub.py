"""A stand-in for `_clone_workspace` that is shaped like the real one.

Production returns `<throwaway>/repo`, and every caller ends with
`shutil.rmtree(clone.parent, ignore_errors=True)` to drop the throwaway. A stub
that returns the seed directory ITSELF therefore aims that rmtree at the seed's
parent — which for a `TemporaryDirectory` is the system temp root. Running the
suite deleted it, and every later `mkdtemp` failed with

    FileNotFoundError: [Errno 2] No such file or directory: '/var/folders/…/T/…'

It went unseen locally because macOS recreates the per-user temp root on
demand; CI, with a plain TMPDIR, failed a hundred tests in one cascade.

Not named test_*.py so unittest discovery leaves it alone.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


def make_clone(seed: Path, *, overlay: dict[str, bytes] | None = None) -> Path:
    """A throwaway copy of `seed`, laid out as production lays out a clone."""
    dest = Path(tempfile.mkdtemp(prefix="shepherd-clonestub-"))
    clone = dest / "repo"
    shutil.copytree(seed, clone, symlinks=True)
    for rel, content in (overlay or {}).items():
        target = clone / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return clone


def clone_stub(seed: Path):
    """Drop-in for `parallel._clone_workspace`."""

    def _clone(repo_root, overlay=None):
        return make_clone(Path(seed), overlay=overlay)

    return _clone


def clone_many_stub(seed: Path):
    """Drop-in for `parallel._clone_many`."""

    def _clone_many(repo_root, n):
        return [make_clone(Path(seed)) for _ in range(n)]

    return _clone_many
