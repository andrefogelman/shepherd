"""Changeset policy: deterministic guards the supervisor applies to every
worker proposal before it is even considered for the test gate.

These guards are code, not LLM judgment. A violation discards the attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChangesetPolicy:
    """Limits a worker proposal must respect."""

    max_changed_paths: int = 40
    max_deleted_paths: int = 3
    allowed_prefixes: tuple[str, ...] = ()  # empty = whole repo allowed
    forbidden_paths: tuple[str, ...] = (
        ".git/",
        ".vcscore/",
        ".env",
        ".venv/",
        "node_modules/",
    )


@dataclass
class PolicyVerdict:
    ok: bool
    violations: list[str] = field(default_factory=list)


def _is_deletion(changeset, path: str) -> bool:
    """read_file returns (bytes, mode) for content and None for a deletion."""
    try:
        return changeset.read_file(path) is None
    except Exception:
        return True


def check_changeset(changeset, policy: ChangesetPolicy) -> PolicyVerdict:
    """Apply deterministic guards to a retained changeset."""
    violations: list[str] = []
    paths = list(changeset.changed_paths)

    if len(paths) > policy.max_changed_paths:
        violations.append(
            f"changed {len(paths)} paths (max {policy.max_changed_paths})"
        )

    deletions = [p for p in paths if _is_deletion(changeset, p)]
    if len(deletions) > policy.max_deleted_paths:
        violations.append(
            f"deleted {len(deletions)} paths (max {policy.max_deleted_paths}): "
            + ", ".join(deletions[:10])
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
