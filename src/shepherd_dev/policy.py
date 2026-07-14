"""Changeset policy: deterministic guards the supervisor applies to every
worker proposal before it is even considered for the test gate.

These guards are code, not LLM judgment. A violation discards the attempt.

Guards operate on the proposal's CONTENT entries (files the worker actually
wrote). Worker deletions cannot be expressed in the v0.3.0 workspace lane
(see supervisor.read_changeset_entries), so there is no deletion guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath


@dataclass(frozen=True)
class ChangesetPolicy:
    """Limits a worker proposal must respect."""

    max_changed_paths: int = 40
    allowed_prefixes: tuple[str, ...] = ()  # empty = whole repo allowed
    forbidden_paths: tuple[str, ...] = (
        ".git/",
        ".vcscore/",
        ".shepherd-proposals/",
        ".env",
        ".venv/",
        "node_modules/",
    )


@dataclass
class PolicyVerdict:
    ok: bool
    violations: list[str] = field(default_factory=list)


def _escapes_repo(path: str) -> bool:
    """A proposal path that is absolute or climbs out of the repo (`..`, `~`)."""
    if path.startswith(("/", "~", "\\")):
        return True
    p = PurePosixPath(path)
    return p.is_absolute() or ".." in p.parts


def _is_forbidden(path: str, forbidden_paths: tuple[str, ...]) -> bool:
    """Match forbidden entries against ANY path segment, not just the prefix.

    A forbidden entry ending in `/` (e.g. `node_modules/`) matches that name as
    any directory component (so `pkg/node_modules/x` is caught). A file entry
    (e.g. `.env`) matches the basename exactly or as a prefix (so `.env.local`
    and `config/.env` are caught)."""
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else path
    dirs = {f.rstrip("/") for f in forbidden_paths if f.endswith("/")}
    files = tuple(f for f in forbidden_paths if not f.endswith("/"))
    if any(seg in dirs for seg in parts):
        return True
    return any(name == f or name.startswith(f) for f in files)


def check_paths(paths: list[str], policy: ChangesetPolicy) -> PolicyVerdict:
    """Apply deterministic guards to the proposal's written paths."""
    violations: list[str] = []

    if len(paths) > policy.max_changed_paths:
        violations.append(
            f"changed {len(paths)} paths (max {policy.max_changed_paths})"
        )

    for path in paths:
        if _escapes_repo(path):
            violations.append(f"path escapes the repo: {path}")
            continue  # an escaping path fails hard; further checks are moot
        if _is_forbidden(path, policy.forbidden_paths):
            violations.append(f"touched forbidden path: {path}")
        if policy.allowed_prefixes and not any(
            path.startswith(prefix) for prefix in policy.allowed_prefixes
        ):
            violations.append(f"touched path outside allowed prefixes: {path}")

    return PolicyVerdict(ok=not violations, violations=violations)
