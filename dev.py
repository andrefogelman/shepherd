"""CLI for supervised AI development on top of Shepherd.

Usage:
    # develop (output is held for review, nothing touches your files)
    .venv/bin/python dev.py run "add CPF validation to signup" \
        --repo ~/projetos/foo --test-cmd "npm test"

    # settle a passing proposal (human decision)
    .venv/bin/python dev.py settle <run-ref> --repo ~/projetos/foo            # accept
    .venv/bin/python dev.py settle <run-ref> --repo ~/projetos/foo --reject   # discard

The target repo must be a Shepherd workspace (`shepherd init` inside it once).
Accepting a proposal advances the Shepherd world (select) AND writes the files
into your working tree, keeping both in sync; committing to git stays with you.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import shepherd as sp

from parallel import PROPOSALS_DIR, develop_parallel
from policy import ChangesetPolicy
from supervisor import develop, materialize_into, read_changeset_entries, set_worker_budget
from tasks import implement, review, write_tests


def _resolve_repo(raw: str) -> Path | None:
    repo_root = Path(raw).expanduser().resolve()
    if not repo_root.is_dir():
        print(f"error: repo not found: {repo_root}", file=sys.stderr)
        return None
    if not (repo_root / ".vcscore").exists():
        print(
            f"error: {repo_root} is not a Shepherd workspace. Run once inside it: shepherd init",
            file=sys.stderr,
        )
        return None
    return repo_root


def _refresh_substrate(repo_root: Path) -> str | None:
    """Recreate .vcscore so the run basis equals the current worktree.

    In shepherd-ai 0.3.0 runs fork from the workspace's ORIGINAL adoption
    basis; settlements do not feed later runs' bases (verified empirically).
    Since git is our durable source of truth, each `dev.py run` re-adopts the
    worktree from scratch. Refuses if an unconsumed proposal is still pending.
    Returns an error message, or None on success.
    """
    import shutil
    import subprocess

    vcscore = repo_root / ".vcscore"
    if vcscore.exists():
        with sp.open(repo_root) as workspace:
            pending = []
            for record in workspace.runs.list():
                for output in workspace.runs.outputs(run_ref=record.run_ref):
                    if output.state == "unconsumed":
                        pending.append(record.run_ref)
        if pending:
            return (
                "pending unconsumed proposal(s): "
                + ", ".join(sorted(set(pending)))
                + " — settle them first (dev.py settle <ref> [--reject])"
            )
        shutil.rmtree(vcscore)

    shepherd_bin = Path(sys.executable).parent / "shepherd"
    proc = subprocess.run(
        [str(shepherd_bin), "init"], cwd=repo_root, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return f"shepherd init failed: {proc.stderr.strip() or proc.stdout.strip()}"
    return None


def cmd_run(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2

    error = _refresh_substrate(repo_root)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.provider == "claude":
        set_worker_budget(args.worker_budget)

    policy = ChangesetPolicy(
        max_changed_paths=args.max_changed_paths,
        allowed_prefixes=tuple(args.allowed_prefix),
    )
    placement = "jail" if args.provider == "claude" else "advisory"
    worker = implement if args.mode == "feature" else write_tests
    # reviewer needs a live model; skip it on the deterministic provider
    reviewer = None if (args.no_review or args.provider == "static") else review

    with sp.open(repo_root) as workspace:
        report = develop(
            workspace,
            worker,
            repo=workspace.git_repo(),
            repo_root=repo_root,
            feature=args.feature,
            test_cmd=args.test_cmd,
            provider=args.provider,
            placement=placement,
            max_attempts=args.max_attempts,
            gate_timeout=args.gate_timeout,
            policy=policy,
            review_task=reviewer,
        )

    print(report.summary())
    return 0 if report.succeeded else 1


def cmd_run2(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2

    if args.provider == "claude":
        set_worker_budget(args.worker_budget)

    policy = ChangesetPolicy(
        max_changed_paths=args.max_changed_paths,
        allowed_prefixes=tuple(args.allowed_prefix),
    )
    placement = "jail" if args.provider == "claude" else "advisory"
    reviewer = None if (args.no_review or args.provider == "static") else review

    report = develop_parallel(
        repo_root,
        [args.feature_a, args.feature_b],
        test_cmd=args.test_cmd,
        provider=args.provider,
        placement=placement,
        policy=policy,
        max_attempts=args.max_attempts,
        max_repairs=args.max_repairs,
        gate_timeout=args.gate_timeout,
        review_task=reviewer,
    )
    print(report.summary())
    return 0 if report.succeeded else 1


def cmd_settle_par(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2
    staging = repo_root / PROPOSALS_DIR / args.proposal_id
    files_dir = staging / "files"
    if not files_dir.is_dir():
        print(f"error: staged proposal not found: {staging}", file=sys.stderr)
        return 2

    if args.reject:
        import shutil

        shutil.rmtree(staging)
        print(f"{args.proposal_id}: staged proposal discarded")
        return 0

    entries = {
        str(path.relative_to(files_dir)): path.read_bytes()
        for path in files_dir.rglob("*")
        if path.is_file()
    }
    written = materialize_into(repo_root, entries)
    import shutil

    shutil.rmtree(staging)
    print(f"{args.proposal_id}: accepted — {len(written)} file(s) written:")
    for rel in written:
        print(f"  {rel}")
    print("review and commit them with git.")
    return 0


def cmd_settle(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2

    with sp.open(repo_root) as workspace:
        outputs = [
            o for o in workspace.runs.outputs(run_ref=args.run_ref) if o.output_name == "workspace"
        ]
        if not outputs:
            print(f"error: no workspace output found for {args.run_ref}", file=sys.stderr)
            return 2
        output = outputs[0]

        state = output.state
        if state != "unconsumed":
            print(
                f"error: {args.run_ref} output is already consumed (state={state!r}); "
                "settlement verbs are consume-once",
                file=sys.stderr,
            )
            return 2

        if args.reject:
            output.discard()
            print(f"{args.run_ref}: proposal discarded")
            return 0

        # Snapshot the changeset BEFORE selecting (settlement consumes the output).
        entries = read_changeset_entries(output.changeset())
        if not entries:
            print(f"error: {args.run_ref} has an empty changeset; nothing to accept", file=sys.stderr)
            return 2
        output.select()

    # Mirror files only after the workspace closes: while it is active, vcs-core
    # blocks unscoped mutations of workspace files (UnscopedMutationError).
    written = materialize_into(repo_root, entries)

    print(f"{args.run_ref}: accepted — world advanced, {len(written)} file(s) written:")
    for rel in written:
        print(f"  {rel}")
    print("review and commit them with git.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervised AI development via Shepherd")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="develop a feature under supervision")
    p_run.add_argument("feature", help="feature request in natural language")
    p_run.add_argument("--repo", required=True, help="path to the target repo (shepherd-initialized)")
    p_run.add_argument("--test-cmd", required=True, help='test gate command, e.g. "pytest -q"')
    p_run.add_argument("--provider", default="claude", choices=["claude", "static"])
    p_run.add_argument(
        "--mode",
        default="feature",
        choices=["feature", "tests"],
        help="feature: implement the request; tests: only write/update tests for it",
    )
    p_run.add_argument(
        "--no-review",
        action="store_true",
        help="skip the reviewer pass after the gate passes",
    )
    p_run.add_argument("--max-attempts", type=int, default=3)
    p_run.add_argument("--gate-timeout", type=int, default=600, help="seconds for the test suite")
    p_run.add_argument(
        "--worker-budget",
        type=int,
        default=900,
        help="wall-clock seconds each worker attempt may use (claude provider)",
    )
    p_run.add_argument("--max-changed-paths", type=int, default=40)
    p_run.add_argument(
        "--allowed-prefix",
        action="append",
        default=[],
        help="restrict changes to this path prefix (repeatable)",
    )
    p_run.set_defaults(func=cmd_run)

    p_run2 = sub.add_parser("run2", help="develop two features with parallel coordinated workers")
    p_run2.add_argument("feature_a", help="first feature (leader on conflicts)")
    p_run2.add_argument("feature_b", help="second feature (reworks on conflicts)")
    p_run2.add_argument("--repo", required=True, help="path to the target repo (shepherd-initialized)")
    p_run2.add_argument("--test-cmd", required=True, help="combined test gate command")
    p_run2.add_argument("--provider", default="claude", choices=["claude", "static"])
    p_run2.add_argument("--no-review", action="store_true")
    p_run2.add_argument("--max-attempts", type=int, default=2, help="attempts per worker")
    p_run2.add_argument("--max-repairs", type=int, default=2, help="repair rounds on the combined gate")
    p_run2.add_argument("--gate-timeout", type=int, default=600)
    p_run2.add_argument("--worker-budget", type=int, default=900)
    p_run2.add_argument("--max-changed-paths", type=int, default=40)
    p_run2.add_argument("--allowed-prefix", action="append", default=[])
    p_run2.set_defaults(func=cmd_run2)

    p_spar = sub.add_parser("settle-par", help="accept or reject a staged parallel proposal")
    p_spar.add_argument("proposal_id", help="staged proposal id (see run2 output)")
    p_spar.add_argument("--repo", required=True, help="path to the target repo")
    p_spar.add_argument("--reject", action="store_true", help="discard instead of accept")
    p_spar.set_defaults(func=cmd_settle_par)

    p_settle = sub.add_parser("settle", help="accept or reject a retained proposal")
    p_settle.add_argument("run_ref", help="full run ref, e.g. run-fc83a2df3eaa")
    p_settle.add_argument("--repo", required=True, help="path to the target repo")
    p_settle.add_argument("--reject", action="store_true", help="discard instead of accept")
    p_settle.set_defaults(func=cmd_settle)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
