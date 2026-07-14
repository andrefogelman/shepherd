"""Collect file content entries by comparing two directory trees.

Pure stdlib — no shepherd-ai. Used by the Grok host worker (L1) to turn a
modified worktree into the same `dict[str, bytes]` shape the gate/policy expect.
Deletions are not represented (same limitation as the v0.3.0 workspace lane).
"""

from __future__ import annotations

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


def collect_changed_entries(
    original: Path,
    modified: Path,
    *,
    ignore_dirs: set[str] | None = None,
) -> dict[str, bytes]:
    """Return relative-path → bytes for files that are new or differ in `modified`."""
    ignore = ignore_dirs if ignore_dirs is not None else DEFAULT_IGNORE_DIRS
    original = original.resolve()
    modified = modified.resolve()
    entries: dict[str, bytes] = {}
    if not modified.is_dir():
        return entries
    for path in modified.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(modified)
        if any(part in ignore for part in rel.parts):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        orig = original / rel
        try:
            if orig.is_file() and not orig.is_symlink() and orig.read_bytes() == data:
                continue
        except OSError:
            pass
        entries[str(rel.as_posix())] = data
    return entries
