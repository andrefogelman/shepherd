"""Collect file content entries by comparing two directory trees.

Pure stdlib — no shepherd-ai. Used by the Grok host worker (L1) to turn a
modified worktree into the same `dict[str, bytes]` shape the gate/policy expect.
Deletions are not represented (same limitation as the v0.3.0 workspace lane).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

# Match supervisor IGNORED_DIRS + VCS noise when walking trees.
DEFAULT_IGNORE_DIRS = {
    ".vcscore",
    ".venv",
    "node_modules",
    "__pycache__",
    ".shepherd",
    ".review",
    ".shepherd-proposals",
    ".git",
    ".grok",
    ".tokensave",
}


def _walk(root: Path, ignore: set[str]):
    """Yield (relative-posix-path, path) for every regular file under `root`."""
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if any(part in ignore for part in rel.parts):
            continue
        yield rel.as_posix(), path


@dataclass(frozen=True)
class _Entry:
    sha: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class TreeSnapshot:
    """What a tree contained at one instant: content hash plus the stat pair
    that lets a later comparison skip re-reading files nothing touched."""

    entries: dict[str, _Entry]
    taken_ns: int

    def __contains__(self, rel: str) -> bool:
        return rel in self.entries

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def unchanged(self, rel: str, st) -> bool:
        """True when `st` matches the snapshot closely enough to skip the read.

        Size AND mtime must both match — the same evidence make and git's index
        rely on. A file stamped at or after the snapshot instant is NOT trusted:
        filesystem timestamp granularity means a write inside that window can
        land on the recorded mtime, so those are re-read and hashed (git calls
        this the racily-clean case).
        """
        prior = self.entries.get(rel)
        return (
            prior is not None
            and prior.size == st.st_size
            and prior.mtime_ns == st.st_mtime_ns
            and st.st_mtime_ns < self.taken_ns
        )

    def matches(self, rel: str, data: bytes) -> bool:
        prior = self.entries.get(rel)
        return prior is not None and prior.sha == hashlib.sha256(data).hexdigest()


def snapshot_tree(root: Path, *, ignore_dirs: set[str] | None = None) -> TreeSnapshot:
    """Snapshot every regular file under `root` (sha256 + size + mtime).

    Taken right after the worker's clone is made, this pins the base the
    proposal is later diffed against. Without it the diff compares the clone to
    the LIVE repo, so a file the human edits mid-run looks "changed" and enters
    the proposal carrying the worker's stale copy — settling then reverts the
    human's edit in silence (#3).
    """
    ignore = ignore_dirs if ignore_dirs is not None else DEFAULT_IGNORE_DIRS
    entries: dict[str, _Entry] = {}
    for rel, path in _walk(root.resolve(), ignore):
        try:
            st = path.stat()
            entries[rel] = _Entry(
                sha=hashlib.sha256(path.read_bytes()).hexdigest(),
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
            )
        except OSError:
            continue
    return TreeSnapshot(entries=entries, taken_ns=time.time_ns())


def collect_changed_entries(
    original: Path,
    modified: Path,
    *,
    ignore_dirs: set[str] | None = None,
    baseline: TreeSnapshot | None = None,
) -> dict[str, bytes]:
    """Return relative-path → bytes for files that are new or differ in `modified`.

    `baseline` (from ``snapshot_tree`` at clone time) takes precedence over
    `original` when given: it — not the live tree — decides what counts as
    changed, so a concurrent edit to `original` cannot pull an untouched file
    into the result (#3). It also lets an untouched file be dismissed on its
    stat alone, instead of reading every file of both trees on every attempt.
    """
    ignore = ignore_dirs if ignore_dirs is not None else DEFAULT_IGNORE_DIRS
    original = original.resolve()
    modified = modified.resolve()
    entries: dict[str, bytes] = {}
    for rel, path in _walk(modified, ignore):
        if baseline is not None:
            try:
                if baseline.unchanged(rel, path.stat()):
                    continue  # the worker never touched it: no read, no hash
            except OSError:
                pass
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if baseline is not None:
            if baseline.matches(rel, data):
                continue
        else:
            orig = original / rel
            try:
                if orig.is_file() and not orig.is_symlink() and orig.read_bytes() == data:
                    continue
            except OSError:
                pass
        entries[rel] = data
    return entries
