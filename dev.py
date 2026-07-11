"""CLI for supervised AI development on top of Shepherd.

Usage:
    .venv/bin/python dev.py "add CPF validation to signup" \
        --repo ~/projetos/foo --test-cmd "npm test"

The target repo must be a Shepherd workspace (`shepherd init` inside it once).
Worker output is held for review; nothing touches the repo until you settle
the run with `shepherd run select/apply/discard <ref>`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import shepherd as sp

from policy import ChangesetPolicy
from supervisor import develop
from tasks import implement


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervised AI development via Shepherd")
    parser.add_argument("feature", help="feature request in natural language")
    parser.add_argument("--repo", required=True, help="path to the target repo (shepherd-initialized)")
    parser.add_argument("--test-cmd", required=True, help='test gate command, e.g. "pytest -q"')
    parser.add_argument("--provider", default="claude", choices=["claude", "static"])
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--gate-timeout", type=int, default=600, help="seconds for the test suite")
    parser.add_argument("--max-changed-paths", type=int, default=40)
    parser.add_argument("--max-deleted-paths", type=int, default=3)
    parser.add_argument(
        "--allowed-prefix",
        action="append",
        default=[],
        help="restrict changes to this path prefix (repeatable)",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo).expanduser().resolve()
    if not repo_root.is_dir():
        print(f"error: repo not found: {repo_root}", file=sys.stderr)
        return 2
    if not (repo_root / ".vcscore").exists():
        print(
            f"error: {repo_root} is not a Shepherd workspace. "
            f"Run once inside it: shepherd init",
            file=sys.stderr,
        )
        return 2

    policy = ChangesetPolicy(
        max_changed_paths=args.max_changed_paths,
        max_deleted_paths=args.max_deleted_paths,
        allowed_prefixes=tuple(args.allowed_prefix),
    )
    placement = "jail" if args.provider == "claude" else "advisory"

    with sp.open(repo_root) as workspace:
        report = develop(
            workspace,
            implement,
            repo=workspace.git_repo(),
            repo_root=repo_root,
            feature=args.feature,
            test_cmd=args.test_cmd,
            provider=args.provider,
            placement=placement,
            max_attempts=args.max_attempts,
            gate_timeout=args.gate_timeout,
            policy=policy,
        )

    print(report.summary())
    return 0 if report.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
