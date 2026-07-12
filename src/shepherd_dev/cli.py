"""CLI for supervised AI development on top of Shepherd.

Usage:
    # develop (output is held for review, nothing touches your files)
    shepherd-dev run "add CPF validation to signup" \
        --repo ~/projetos/foo --test-cmd "npm test"

    # settle a passing proposal (human decision)
    shepherd-dev settle <run-ref> --repo ~/projetos/foo            # accept
    shepherd-dev settle <run-ref> --repo ~/projetos/foo --reject   # discard

The target repo must be a Shepherd workspace (`shepherd init` inside it once).
Accepting a proposal advances the Shepherd world (select) AND writes the files
into your working tree, keeping both in sync; committing to git stays with you.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import shepherd as sp

from . import history
from .parallel import PROPOSALS_DIR, develop_best_of, develop_parallel
from .policy import ChangesetPolicy
from .supervisor import develop, materialize_into, read_changeset_entries, set_worker_budget
from .tasks import implement, review, write_tests


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
    Since git is our durable source of truth, each `shepherd-dev run` re-adopts the
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
                + " — settle them first (shepherd-dev settle <ref> [--reject])"
            )
        shutil.rmtree(vcscore)

    shepherd_bin = Path(sys.executable).parent / "shepherd"
    proc = subprocess.run(
        [str(shepherd_bin), "init"], cwd=repo_root, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return f"shepherd init failed: {proc.stderr.strip() or proc.stdout.strip()}"
    return None


def _slugify(text: str, limit: int = 28) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "feature"


def auto_commit_branch(repo_root: Path, written: list[str], slug: str, message: str) -> tuple[str | None, str | None]:
    """Commit accepted files on an isolated shepherd/<slug> branch, then return
    to the original branch. Never pushes. Returns (branch_name, error)."""
    import subprocess

    def git(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *argv], cwd=repo_root, capture_output=True, text=True)

    cur = git("rev-parse", "--abbrev-ref", "HEAD")
    original = cur.stdout.strip()
    if cur.returncode != 0 or original == "HEAD":
        return None, "not on a branch (detached HEAD) — files left in the working tree, commit manually"

    branch = f"shepherd/{slug}"
    n = 2
    while git("rev-parse", "--verify", "--quiet", branch).returncode == 0:
        branch = f"shepherd/{slug}-{n}"
        n += 1

    steps = [
        ("checkout -b", git("checkout", "-b", branch)),
        ("add", git("add", "--", *written)),
        ("commit", git("commit", "-m", message)),
        ("checkout back", git("checkout", original)),
    ]
    for name, proc in steps:
        if proc.returncode != 0:
            return None, f"git {name} failed: {(proc.stderr or proc.stdout).strip()[:300]}"
    return branch, None


def _auto_settle_conditions(report) -> str | None:
    """None = all hard conditions met; otherwise the human-readable reason."""
    if not report.succeeded:
        return "run did not succeed"
    if report.review is None:
        return "no review was run"
    if report.review.error:
        return f"review unavailable: {report.review.error}"
    if not report.review.approved:
        return "review REJECTED the proposal"
    return None


def _run_best_of(args, repo_root: Path, worker, reviewer, policy, placement) -> int:
    report = develop_best_of(
        repo_root,
        args.feature,
        k=args.best_of,
        test_cmd=args.test_cmd,
        provider=args.provider,
        placement=placement,
        policy=policy,
        max_attempts=args.max_attempts,
        gate_timeout=args.gate_timeout,
        review_task=reviewer,
        worker_task=worker,
    )
    history.record_event(
        "best_of",
        {
            "feature": args.feature,
            "repo": str(repo_root),
            "k": args.best_of,
            "succeeded": report.succeeded,
            "winner": report.winner_index,
            "proposal_id": report.proposal_id,
            "candidates": [
                {
                    "index": c.index,
                    "verdict": c.verdict,
                    "gate_passed": c.gate_passed,
                    "review_approved": (c.review.approved if c.review else None),
                    "files": c.files,
                    "diff_bytes": c.diff_bytes,
                }
                for c in report.candidates
            ],
            "flags": {"auto_settle": args.auto_settle, "mode": args.mode},
        },
    )
    print(report.summary())

    if args.auto_settle and report.proposal_id:
        reason = _auto_settle_conditions(report)
        if reason:
            print(f"\nauto-settle: NOT applied ({reason}) — proposal stays staged for manual settlement")
            return 0 if report.succeeded else 1
        code, written = settle_proposal(repo_root, report.proposal_id, reject=False, auto=True)
        if code != 0 or not written:
            return 1
        branch, err = auto_commit_branch(
            repo_root, written, _slugify(args.feature),
            f"feat: {args.feature}\n\nshepherd-dev auto-settle (best-of-{args.best_of}, "
            f"proposal {report.proposal_id}); gate passed, review approved.",
        )
        if err:
            print(f"auto-settle: files written but NOT committed — {err}")
        else:
            print(f"auto-settle: committed on branch {branch} (current branch untouched, no push)")
        return 0

    return 0 if report.succeeded else 1


def cmd_run(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2

    if args.auto_settle and args.no_review:
        print("error: --auto-settle requires the reviewer (drop --no-review)", file=sys.stderr)
        return 2
    if args.auto_settle and args.provider == "static":
        print("error: --auto-settle requires the claude provider (review is mandatory)", file=sys.stderr)
        return 2

    if args.best_of > 1 and args.no_review and args.provider != "static":
        print("error: --best-of needs the reviewer for ranking (drop --no-review)", file=sys.stderr)
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

    if args.best_of > 1:
        return _run_best_of(args, repo_root, worker, reviewer, policy, placement)

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

    history.record_event(
        "run",
        history.run_payload(
            report, repo_root,
            mode=args.mode, test_cmd=args.test_cmd, provider=args.provider,
            flags={
                "max_attempts": args.max_attempts,
                "allowed_prefix": args.allowed_prefix,
                "auto_settle": args.auto_settle,
            },
        ),
    )
    print(report.summary())

    if args.auto_settle and report.final_run_ref:
        reason = _auto_settle_conditions(report)
        if reason:
            print(f"\nauto-settle: NOT applied ({reason}) — proposal stays retained for manual settlement")
            return 0 if report.succeeded else 1
        code, written = settle_run(repo_root, report.final_run_ref, reject=False, auto=True)
        if code != 0 or not written:
            return 1
        branch, err = auto_commit_branch(
            repo_root, written, _slugify(args.feature),
            f"feat: {args.feature}\n\nshepherd-dev auto-settle ({report.final_run_ref}); "
            f"gate passed, review approved.",
        )
        if err:
            print(f"auto-settle: files written but NOT committed — {err}")
        else:
            print(f"auto-settle: committed on branch {branch} (current branch untouched, no push)")
        return 0

    return 0 if report.succeeded else 1


def cmd_run2(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2

    if args.auto_settle and args.no_review:
        print("error: --auto-settle requires the reviewer (drop --no-review)", file=sys.stderr)
        return 2
    if args.auto_settle and args.provider == "static":
        print("error: --auto-settle requires the claude provider (review is mandatory)", file=sys.stderr)
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
    history.record_event(
        "run2",
        history.parallel_payload(
            report, repo_root,
            test_cmd=args.test_cmd, provider=args.provider,
            flags={
                "max_attempts": args.max_attempts,
                "max_repairs": args.max_repairs,
                "auto_settle": args.auto_settle,
            },
        ),
    )
    print(report.summary())

    if args.auto_settle and report.proposal_id:
        reason = _auto_settle_conditions(report)
        if reason:
            print(f"\nauto-settle: NOT applied ({reason}) — proposal stays staged for manual settlement")
            return 0 if report.succeeded else 1
        code, written = settle_proposal(repo_root, report.proposal_id, reject=False, auto=True)
        if code != 0 or not written:
            return 1
        branch, err = auto_commit_branch(
            repo_root, written, _slugify(f"{args.feature_a}-{args.feature_b}"),
            f"feat: {args.feature_a} + {args.feature_b}\n\nshepherd-dev auto-settle "
            f"(proposal {report.proposal_id}); combined gate passed, review approved.",
        )
        if err:
            print(f"auto-settle: files written but NOT committed — {err}")
        else:
            print(f"auto-settle: committed on branch {branch} (current branch untouched, no push)")
        return 0

    return 0 if report.succeeded else 1


def settle_proposal(repo_root: Path, proposal_id: str, *, reject: bool, auto: bool = False) -> tuple[int, list[str]]:
    """Core settlement for a staged run2/best-of proposal. Returns (exit_code, written)."""
    import shutil

    staging = repo_root / PROPOSALS_DIR / proposal_id
    files_dir = staging / "files"
    if not files_dir.is_dir():
        print(f"error: staged proposal not found: {staging}", file=sys.stderr)
        return 2, []

    if reject:
        shutil.rmtree(staging)
        history.record_event(
            "settle_par",
            {"repo": str(repo_root), "ref": proposal_id, "action": "reject", "auto": auto},
        )
        print(f"{proposal_id}: staged proposal discarded")
        return 0, []

    entries = {
        str(path.relative_to(files_dir)): path.read_bytes()
        for path in files_dir.rglob("*")
        if path.is_file()
    }
    written = materialize_into(repo_root, entries)
    shutil.rmtree(staging)
    history.record_event(
        "settle_par",
        {"repo": str(repo_root), "ref": proposal_id, "action": "accept", "auto": auto, "written": written},
    )
    print(f"{proposal_id}: accepted — {len(written)} file(s) written:")
    for rel in written:
        print(f"  {rel}")
    return 0, written


def cmd_settle_par(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2
    code, written = settle_proposal(repo_root, args.proposal_id, reject=args.reject)
    if code == 0 and written:
        print("review and commit them with git.")
    return code


GITIGNORE_ENTRIES = (".vcscore/", "REVIEW.json", ".shepherd-proposals/")


def _ensure_gitignore(repo_root: Path) -> list[str]:
    """Append the Shepherd state entries to the repo's .gitignore, once.

    Idempotent (skips lines already present). Returns the entries it added.
    """
    path = repo_root / ".gitignore"
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    present = {ln.strip() for ln in existing_lines}
    missing = [e for e in GITIGNORE_ENTRIES if e not in present]
    if not missing:
        return []
    block: list[str] = []
    if existing_lines and existing_lines[-1].strip():
        block.append("")
    block.append("# shepherd-dev local state")
    block.extend(missing)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(("\n".join(block)) + "\n")
    return missing


def cmd_init(args) -> int:
    """Initialize a repo as a Shepherd workspace AND gitignore its local state.

    The `shepherd` console script lives inside this tool's venv (dependency
    shepherd-ai) but is not exposed on the user's PATH by uv/pipx — only
    shepherd-dev is. This subcommand bridges that, then wires .gitignore so the
    user does not have to.
    """
    import subprocess

    repo_root = Path(args.repo).expanduser().resolve()
    if not repo_root.is_dir():
        print(f"error: repo not found: {repo_root}", file=sys.stderr)
        return 2
    shepherd_bin = Path(sys.executable).parent / "shepherd"
    proc = subprocess.run([str(shepherd_bin), "init"], cwd=repo_root)
    if proc.returncode != 0:
        return proc.returncode

    if args.no_gitignore:
        print("skipped .gitignore (--no-gitignore); add: " + "  ".join(GITIGNORE_ENTRIES))
    else:
        added = _ensure_gitignore(repo_root)
        if added:
            print(f"gitignored: {'  '.join(added)}")
        else:
            print(".gitignore already covers the shepherd-dev state")
    return 0


def settle_run(repo_root: Path, run_ref: str, *, reject: bool, auto: bool = False) -> tuple[int, list[str]]:
    """Core settlement for a retained run output. Returns (exit_code, written_paths)."""
    with sp.open(repo_root) as workspace:
        outputs = [
            o for o in workspace.runs.outputs(run_ref=run_ref) if o.output_name == "workspace"
        ]
        if not outputs:
            print(f"error: no workspace output found for {run_ref}", file=sys.stderr)
            return 2, []
        output = outputs[0]

        state = output.state
        if state != "unconsumed":
            print(
                f"error: {run_ref} output is already consumed (state={state!r}); "
                "settlement verbs are consume-once",
                file=sys.stderr,
            )
            return 2, []

        if reject:
            output.discard()
            history.record_event(
                "settle", {"repo": str(repo_root), "ref": run_ref, "action": "reject", "auto": auto}
            )
            print(f"{run_ref}: proposal discarded")
            return 0, []

        # Snapshot the changeset BEFORE selecting (settlement consumes the output).
        entries = read_changeset_entries(output.changeset())
        if not entries:
            print(f"error: {run_ref} has an empty changeset; nothing to accept", file=sys.stderr)
            return 2, []
        output.select()

    # Mirror files only after the workspace closes: while it is active, vcs-core
    # blocks unscoped mutations of workspace files (UnscopedMutationError).
    written = materialize_into(repo_root, entries)
    history.record_event(
        "settle",
        {"repo": str(repo_root), "ref": run_ref, "action": "accept", "auto": auto, "written": written},
    )

    print(f"{run_ref}: accepted — world advanced, {len(written)} file(s) written:")
    for rel in written:
        print(f"  {rel}")
    return 0, written


def cmd_optimize(args) -> int:
    from .optimize import optimize

    report = optimize(
        fix_n=args.fix_n, guard_n=args.guard_n, model=args.model,
        worker_budget=args.worker_budget, apply=args.apply,
    )
    print(report.summary())
    return 0


def cmd_settle(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2
    code, written = settle_run(repo_root, args.run_ref, reject=args.reject)
    if code == 0 and written:
        print("review and commit them with git.")
    return code


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
    p_run.add_argument(
        "--auto-settle",
        action="store_true",
        help="on gate PASS + review APPROVED: settle and commit on an isolated shepherd/<slug> branch (never pushes)",
    )
    p_run.add_argument(
        "--best-of",
        type=int,
        default=1,
        choices=[1, 2, 3, 4],
        help="branch K candidates from the same state, gate+review all, stage the winner (Tree-RL essence at inference)",
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
    p_run2.add_argument(
        "--auto-settle",
        action="store_true",
        help="on combined gate PASS + review APPROVED: settle and commit on an isolated shepherd/<slug> branch (never pushes)",
    )
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

    p_init = sub.add_parser("init", help="initialize a repo as a Shepherd workspace + gitignore its state (one-time)")
    p_init.add_argument("--repo", default=".", help="path to the target repo (default: cwd)")
    p_init.add_argument("--no-gitignore", action="store_true", help="do not touch .gitignore")
    p_init.set_defaults(func=cmd_init)

    p_opt = sub.add_parser("optimize", help="CRO-lite: mine run history, propose a prompt edit, validate by replay")
    p_opt.add_argument("--fix-n", type=int, default=3, help="past failures to replay (must improve)")
    p_opt.add_argument("--guard-n", type=int, default=3, help="past passes to replay (must not regress)")
    p_opt.add_argument("--model", default="claude-opus-4-8", help="meta-optimizer model")
    p_opt.add_argument("--worker-budget", type=int, default=900)
    p_opt.add_argument("--apply", action="store_true", help="persist the edit if it passes (default: dry-run)")
    p_opt.set_defaults(func=cmd_optimize)

    args = parser.parse_args()
    return args.func(args)


def entry() -> None:
    """Console-script entry point (pyproject [project.scripts])."""
    sys.exit(main())


if __name__ == "__main__":
    entry()
