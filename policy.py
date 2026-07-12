"""Changeset policy: deterministic guards the supervisor applies to every
worker proposal before it is even considered for the test gate.

These guards are code, not LLM judgment. A violation discards the attempt.

Guards operate on the proposal's CONTENT entries (files the worker actually
wrote). Worker deletions cannot be expressed in the v0.3.0 workspace lane
(see supervisor.read_changeset_entries), so there is no deletion guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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


def check_paths(paths: list[str], policy: ChangesetPolicy) -> PolicyVerdict:
    """Apply deterministic guards to the proposal's written paths."""
    violations: list[str] = []

    if len(paths) > policy.max_changed_paths:
        violations.append(
            f"changed {len(paths)} paths (max {policy.max_changed_paths})"
        )

    for path in paths:
        for forbidden in policy.forbidden_paths:
            if path == forbidden.rstrip("/") or path.startswith(forbidden):
                violations.append(f"touched forbidden path: {path}")
        if policy.allowed_prefixes and not any(
            path.startswith(prefix) for prefix in policy.allowed_prefixes
        ):
            violations.append(f"touched path outside allowed prefixes: {path}")

    return PolicyVerdict(ok=not violations, violations=violations)
