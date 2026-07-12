"""Parallel coordinator: two workers develop complementary features in
isolated workspace clones; a supervisor coordinates conflicts (handoff),
gates the COMBINED proposal, reviews it, and stages it for human settlement.

Practical replica of the paper's runtime-supervisor application (arXiv
2605.10913 §runtime supervision) on the v0.3.0 workspace lane: workspace
activation is exclusive per directory, so parallelism uses one ephemeral
clone per worker instead of low-level Scope forks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import shepherd as sp

from .policy import ChangesetPolicy
from .supervisor import (
    IGNORED_DIRS,
    DevReport,
    GateResult,
    ReviewVerdict,
    _run_gate,
    develop,
    materialize_into,
    run_review,
)
from .tasks import implement

PROPOSALS_DIR = ".shepherd-proposals"


@dataclass
class ParallelReport:
    features: list[str]
    succeeded: bool
    workers: list[DevReport] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    handoff_used: bool = False
    repairs: int = 0
    combined_gate: GateResult | None = None
    review: ReviewVerdict | None = None
    proposal_id: str | None = None
    staged_paths: list[str] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> str:
        lines = [f"parallel features: {self.features}", f"succeeded: {self.succeeded}"]
        if self.error:
            lines.append(f"error: {self.error}")
        for i, w in enumerate(self.workers):
            head = w.attempts[-1] if w.attempts else None
            verdict = head.verdict if head else "did not run"
            lines.append(f"  worker {i + 1}: {verdict} ({len(w.entries or {})} file(s))")
        if self.conflicts:
            lines.append(f"conflicts on: {', '.join(self.conflicts)} (handoff={'yes' if self.handoff_used else 'no'})")
        if self.combined_gate:
            g = self.combined_gate
            lines.append(f"combined gate: {'PASS' if g.passed else 'FAIL'} (repairs={self.repairs})")
            if not g.passed:
                lines.append(f"  {g.infra_error or g.output_tail[-500:]}")
        if self.review:
            if self.review.error:
                lines.append(f"review: UNAVAILABLE ({self.review.error})")
            else:
                lines.append(
                    f"review: {'APPROVED' if self.review.approved else 'REJECTED'} — {self.review.summary}"
                )
                lines += [f"  issue: {i}" for i in self.review.issues]
        if self.proposal_id:
            lines += [
                "",
                f"combined proposal staged: {PROPOSALS_DIR}/{self.proposal_id} ({len(self.staged_paths)} file(s))",
                f"  shepherd-dev settle-par {self.proposal_id} --repo <repo>            # accept: write files",
                f"  shepherd-dev settle-par {self.proposal_id} --repo <repo> --reject   # discard",
            ]
        return "\n".join(lines)


def _clone_workspace(repo_root: Path, overlay: dict[str, bytes] | None = None) -> Path:
    """Ephemeral worker clone: worktree copy (+ optional overlay) + shepherd init."""
    dest = Path(tempfile.mkdtemp(prefix="shepherd-par-"))
    clone = dest / "repo"
    shutil.copytree(
        repo_root,
        clone,
        ignore=shutil.ignore_patterns(*IGNORED_DIRS, ".git"),
        symlinks=True,
    )
    if overlay:
        materialize_into(clone, overlay)
    shepherd_bin = Path(sys.executable).parent / "shepherd"
    proc = subprocess.run([str(shepherd_bin), "init"], cwd=clone, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"shepherd init failed in clone: {proc.stderr.strip()}")
    return clone


def _run_worker(
    clone: Path,
    feature: str,
    teammate_note: str,
    *,
    task,
    extra_args: dict | None,
    provider: str,
    placement: str,
    policy: ChangesetPolicy,
    max_attempts: int,
) -> DevReport:
    with sp.open(clone) as workspace:
        report = develop(
            workspace,
            task,
            repo=workspace.git_repo(),
            repo_root=clone,
            feature=feature,
            test_cmd=None,  # combined gate judges the merged proposal
            provider=provider,
            placement=placement,
            max_attempts=max_attempts,
            policy=policy,
            extra_args=extra_args,
            initial_guidance=teammate_note if extra_args is None else "",
        )
    return report


def _entries_diff_text(entries: dict[str, bytes], limit: int = 60_000) -> str:
    parts = [
        f"=== FILE: {rel} (proposed content) ===\n{content.decode('utf-8', errors='replace')}"
        for rel, content in sorted(entries.items())
    ]
    text = "\n\n".join(parts)
    return text[:limit] + (f"\n\n[... truncated at {limit} chars ...]" if len(text) > limit else "")


def develop_parallel(
    repo_root: Path,
    features: list[str],
    *,
    test_cmd: str,
    provider: str = "claude",
    placement: str = "jail",
    policy: ChangesetPolicy | None = None,
    max_attempts: int = 2,
    max_repairs: int = 2,
    gate_timeout: int = 600,
    review_task=None,
    worker_tasks: list | None = None,
    worker_extra_args: list[dict | None] | None = None,
) -> ParallelReport:
    """Coordinate two parallel workers into one gated, reviewed, staged proposal.

    worker_tasks / worker_extra_args exist for the offline static smoke; real
    use runs the `implement` task for both workers.
    """
    assert len(features) == 2, "exactly two parallel features"
    policy = policy or ChangesetPolicy()
    report = ParallelReport(features=list(features), succeeded=False)
    tasks_ = worker_tasks or [implement, implement]
    extras = worker_extra_args or [None, None]
    clones: list[Path] = []

    try:
        clones = [_clone_workspace(repo_root) for _ in range(2)]

        notes = [
            (
                f"CONTEXT: a teammate is implementing IN PARALLEL: {features[1 - i]!r}. "
                "Implement ONLY your feature; keep any shared interfaces compatible "
                "with theirs and do not implement their part."
            )
            for i in range(2)
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    _run_worker,
                    clones[i],
                    features[i],
                    notes[i],
                    task=tasks_[i],
                    extra_args=extras[i],
                    provider=provider,
                    placement=placement,
                    policy=policy,
                    max_attempts=max_attempts,
                )
                for i in range(2)
            ]
            report.workers = [f.result() for f in futures]

        if not all(w.succeeded and w.entries for w in report.workers):
            report.error = "one or both workers produced no accepted proposal"
            return report

        entries_a = dict(report.workers[0].entries or {})
        entries_b = dict(report.workers[1].entries or {})

        # Conflict coordination: leader = worker 1; follower reworks on top of
        # the leader's proposal (paper's handoff).
        report.conflicts = sorted(set(entries_a) & set(entries_b))
        if report.conflicts:
            report.handoff_used = True
            handoff_clone = _clone_workspace(repo_root, overlay=entries_a)
            clones.append(handoff_clone)
            handoff_guidance = (
                "HANDOFF: your teammate's changes are ALREADY APPLIED to this "
                f"repository (files: {', '.join(sorted(entries_a))}). Your previous "
                f"attempt conflicted with theirs on: {', '.join(report.conflicts)}. "
                "Re-implement YOUR feature on top of their work without breaking it."
            )
            with sp.open(handoff_clone) as workspace:
                follower = develop(
                    workspace,
                    tasks_[1],
                    repo=workspace.git_repo(),
                    repo_root=handoff_clone,
                    feature=features[1],
                    test_cmd=None,
                    provider=provider,
                    placement=placement,
                    max_attempts=max_attempts,
                    policy=policy,
                    extra_args=extras[1],
                    initial_guidance=handoff_guidance if extras[1] is None else "",
                )
            report.workers[1] = follower
            if not (follower.succeeded and follower.entries):
                report.error = "handoff rework failed"
                return report
            entries_b = dict(follower.entries)

        combined = {**entries_a, **entries_b}

        # Combined gate with bounded repair rounds on a clone seeded with the
        # merged proposal.
        gate = _run_gate(repo_root, combined, test_cmd, gate_timeout)
        while not gate.passed and not gate.infra_error and report.repairs < max_repairs:
            report.repairs += 1
            repair_clone = _clone_workspace(repo_root, overlay=combined)
            clones.append(repair_clone)
            repair_feature = (
                "The combined work of two teammates is applied to this repository "
                f"({', '.join(sorted(combined))}) for the features {features!r}, but the "
                f"test suite fails (exit {gate.exit_code}). Test output (tail):\n"
                f"{gate.output_tail[-2000:]}\nFix the root cause with the minimal change."
            )
            with sp.open(repair_clone) as workspace:
                repair = develop(
                    workspace,
                    implement,
                    repo=workspace.git_repo(),
                    repo_root=repair_clone,
                    feature=repair_feature,
                    test_cmd=None,
                    provider=provider,
                    placement=placement,
                    max_attempts=1,
                    policy=policy,
                )
            if not (repair.succeeded and repair.entries):
                break
            combined.update(repair.entries)
            gate = _run_gate(repo_root, combined, test_cmd, gate_timeout)
        report.combined_gate = gate
        if not gate.passed:
            report.error = gate.infra_error or "combined gate failed after repairs"
            return report

        if review_task is not None:
            with sp.open(clones[0]) as workspace:
                report.review = run_review(
                    workspace,
                    review_task,
                    feature=f"combined proposal for: {features[0]} + {features[1]}",
                    diff_text=_entries_diff_text(combined),
                    provider=provider,
                    placement=placement,
                )

        report.proposal_id = time.strftime("%Y%m%d-%H%M%S")
        staging = repo_root / PROPOSALS_DIR / report.proposal_id
        files_dir = staging / "files"
        written = materialize_into(files_dir, combined)
        manifest = {
            "features": report.features,
            "worker_runs": [w.final_run_ref for w in report.workers],
            "conflicts": report.conflicts,
            "handoff_used": report.handoff_used,
            "repairs": report.repairs,
            "gate": {"passed": gate.passed, "exit_code": gate.exit_code},
            "review": (
                None
                if report.review is None
                else {
                    "approved": report.review.approved,
                    "summary": report.review.summary,
                    "issues": report.review.issues,
                    "error": report.review.error,
                }
            ),
            "paths": sorted(combined),
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        report.staged_paths = written
        report.succeeded = True
        return report
    finally:
        for clone in clones:
            shutil.rmtree(clone.parent, ignore_errors=True)
