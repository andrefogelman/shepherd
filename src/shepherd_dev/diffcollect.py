"""Collect file content entries by comparing two directory trees.

Pure stdlib — no shepherd-ai. Used by the Grok host worker (L1) to turn a
modified worktree into the same `dict[str, bytes]` shape the gate/policy expect.
Deletions are not represented (same limitation as the v0.3.0 workspace lane).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable


class Entries(dict[str, bytes]):
    """rel-path → content, plus which paths carried the executable bit.

    The whole pipeline (diff text, policy, review, remote tar) reads plain
    `dict[str, bytes]`, so the carrier STAYS a dict and every reader keeps
    working unchanged; the executable set rides on the instance and only the
    two write points consult it (materialize_into's chmod, the remote gate's
    TarInfo.mode). A plain dict carries no mode information: writes land with
    the filesystem default — exactly the behavior before this type existed.
    """

    executable: frozenset[str]

    def __init__(self, *args, executable: Iterable[str] = (), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.executable = frozenset(executable)


def as_entries(mapping: dict[str, bytes] | None) -> Entries:
    """An Entries copy of `mapping`, carrying any executable set across.

    `dict(...)` and `{**a, **b}` silently strip the attribute; every merge or
    defensive copy in the pipeline must go through here instead, or the exec
    bit dies at the copy even when both sources had it.
    """
    if mapping is None:
        return Entries()
    return Entries(mapping, executable=getattr(mapping, "executable", ()))

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
    for directory, dirs, files in os.walk(root, followlinks=False):
        # Prune before descent: a node_modules/.git tree must cost one entry,
        # not a traversal and stat of every file it contains.
        dirs[:] = [name for name in dirs if name not in ignore]
        for name in files:
            if name in ignore:
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path.relative_to(root).as_posix(), path


def snapshot_tree(root: Path, *, ignore_dirs: set[str] | None = None) -> dict[str, tuple[str, bool]]:
    """Return relative-path → (sha256 hex, had-exec-bit) per regular file.

    Taken right after the worker's clone is made, this pins the base the
    proposal is later diffed against. Without it the diff compares the clone to
    the LIVE repo, so a file the human edits mid-run looks "changed" and enters
    the proposal carrying the worker's stale copy — settling then reverts the
    human's edit in silence (#3).

    Content hashes, not stat metadata. A (size, mtime) fast-path to dismiss
    untouched files without reading them is UNSOUND here: Linux stamps mtime
    from a coarse clock (one jiffy, 1-4ms), so a same-size rewrite landing in
    the tick the snapshot was taken in is indistinguishable from no write at
    all — and the worker's edit then vanishes from the changeset in silence,
    which is the very failure #3 exists to prevent. Nor can a racy-window
    margin rescue it: a freshly made clone's files are milliseconds old at
    snapshot time, so every one of them falls inside any sound margin and gets
    hashed regardless. The optimization is worth nothing once it is correct.

    The exec bit rides alongside the hash so a mode-only change (the worker's
    whole fix being `chmod +x deploy.sh`) still diffs as a change; content
    comparison alone cannot see it.
    """
    ignore = ignore_dirs if ignore_dirs is not None else DEFAULT_IGNORE_DIRS
    snapshot: dict[str, tuple[str, bool]] = {}
    for rel, path in _walk(root.resolve(), ignore):
        try:
            with path.open("rb") as fh:
                sha = hashlib.file_digest(fh, "sha256").hexdigest()
            is_exec = bool(path.stat().st_mode & 0o111)
            snapshot[rel] = (sha, is_exec)
        except OSError:
            continue
    return snapshot


def collect_changed_entries(
    original: Path,
    modified: Path,
    *,
    ignore_dirs: set[str] | None = None,
    baseline: dict[str, tuple[str, bool]] | None = None,
) -> Entries:
    """Return relative-path → bytes for files that are new or differ in `modified`.

    `baseline` (from ``snapshot_tree`` at clone time) takes precedence over
    `original` when given: it — not the live tree — decides what counts as
    changed, so a concurrent edit to `original` cannot pull an untouched file
    into the result (#3). The comparison is (content, exec-bit): a mode-only
    change enters the proposal too. Without a baseline the comparison is
    content-only against the live tree — mode-only changes are not detectable
    there (documented limitation; every production caller passes a baseline).

    The result is an Entries: each path's CURRENT exec bit is recorded in
    `.executable` for the writers that re-apply modes downstream.
    """
    ignore = ignore_dirs if ignore_dirs is not None else DEFAULT_IGNORE_DIRS
    original = original.resolve()
    modified = modified.resolve()
    entries: Entries = Entries()
    executable: set[str] = set()
    for rel, path in _walk(modified, ignore):
        try:
            is_exec = bool(path.stat().st_mode & 0o111)
        except OSError:
            is_exec = False
        prior = baseline.get(rel) if baseline is not None else None
        try:
            with path.open("rb") as fh:
                # Large unchanged files need a content hash, not a bytes copy
                # the size of the file. Small files keep the one-read path.
                data = fh.read(256 * 1024)
                if prior is not None and len(data) == 256 * 1024:
                    digest = hashlib.sha256(data)
                    while chunk := fh.read(256 * 1024):
                        digest.update(chunk)
                    if prior == (digest.hexdigest(), is_exec):
                        continue
                    fh.seek(0)
                    data = fh.read()
                else:
                    data += fh.read()
        except OSError:
            continue
        if baseline is not None:
            if prior is not None and prior == (hashlib.sha256(data).hexdigest(), is_exec):
                continue
        else:
            orig = original / rel
            try:
                if orig.is_file() and not orig.is_symlink() and orig.read_bytes() == data:
                    continue
            except OSError:
                pass
        entries[rel] = data
        if is_exec:
            executable.add(rel)
    entries.executable = frozenset(executable)
    return entries
