"""Collect file content entries by comparing two directory trees.

Pure stdlib — no shepherd-ai. Used by the Grok host worker (L1) to turn a
modified worktree into the same `dict[str, bytes]` shape the gate/policy expect.
Deletions are not represented (same limitation as the v0.3.0 workspace lane).
"""

from __future__ import annotations

import hashlib
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


def snapshot_tree(root: Path, *, ignore_dirs: set[str] | None = None) -> dict[str, str]:
    """Return relative-path → sha256 hex for every regular file under `root`.

    Taken right after the worker's clone is made, this pins the base the
    proposal is later diffed against. Without it the diff compares the clone to
    the LIVE repo, so a file the human edits mid-run looks "changed" and enters
    the proposal carrying the worker's stale copy — settling then reverts the
    human's edit in silence (#3).
    """
    ignore = ignore_dirs if ignore_dirs is not None else DEFAULT_IGNORE_DIRS
    snapshot: dict[str, str] = {}
    for rel, path in _walk(root.resolve(), ignore):
        try:
            snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return snapshot


def collect_changed_entries(
    original: Path,
    modified: Path,
    *,
    ignore_dirs: set[str] | None = None,
    baseline: dict[str, str] | None = None,
) -> dict[str, bytes]:
    """Return relative-path → bytes for files that are new or differ in `modified`.

    `baseline` (from ``snapshot_tree`` at clone time) takes precedence over
    `original` when given: it — not the live tree — decides what counts as
    changed, so a concurrent edit to `original` cannot pull an untouched file
    into the result (#3).
    """
    ignore = ignore_dirs if ignore_dirs is not None else DEFAULT_IGNORE_DIRS
    original = original.resolve()
    modified = modified.resolve()
    entries: dict[str, bytes] = {}
    for rel, path in _walk(modified, ignore):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if baseline is not None:
            if baseline.get(rel) == hashlib.sha256(data).hexdigest():
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
