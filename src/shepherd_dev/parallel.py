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
    context_pack: str | None = None,
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
            context_pack=context_pack,
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
    context_pack: str | None = None,
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
                    context_pack=context_pack,
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
                    context_pack=context_pack,
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
                    context_pack=context_pack,
                )

        report.proposal_id, report.staged_paths = _stage_proposal(
            repo_root,
            combined,
            {
                "features": report.features,
                "worker_runs": [w.final_run_ref for w in report.workers],
                "conflicts": report.conflicts,
                "handoff_used": report.handoff_used,
                "repairs": report.repairs,
                "gate": {"passed": gate.passed, "exit_code": gate.exit_code},
                "review": _review_manifest(report.review),
            },
        )
        report.succeeded = True
        return report
    finally:
        for clone in clones:
            shutil.rmtree(clone.parent, ignore_errors=True)


def _review_manifest(review: ReviewVerdict | None) -> dict | None:
    if review is None:
        return None
    return {
        "approved": review.approved,
        "summary": review.summary,
        "issues": review.issues,
        "error": review.error,
    }


def _stage_proposal(repo_root: Path, entries: dict[str, bytes], manifest_extra: dict) -> tuple[str, list[str]]:
    """Stage a combined/winning proposal under .shepherd-proposals/<id>/."""
    proposal_id = time.strftime("%Y%m%d-%H%M%S")
    staging = repo_root / PROPOSALS_DIR / proposal_id
    written = materialize_into(staging / "files", entries)
    manifest = {**manifest_extra, "paths": sorted(entries)}
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return proposal_id, written


# ── Best-of-N (phase C): the incorporable essence of the paper's Tree-RL ────
# Branch K candidates from the SAME repo state (one ephemeral clone each, with
# varied emphasis seeds), gate every candidate on the real suite, review the
# survivors, rank deterministically, stage the winner for settlement.

EMPHASES = [
    "",
    "Prioritize the SIMPLEST correct implementation with the smallest possible diff.",
    "Prioritize robustness: handle edge cases and invalid inputs defensively.",
    "Prioritize matching the existing codebase's idioms, naming and structure exactly.",
]


@dataclass
class BestOfCandidate:
    index: int
    succeeded: bool
    run_ref: str | None
    files: int
    diff_bytes: int
    gate_passed: bool
    review: ReviewVerdict | None
    verdict: str  # short human label


@dataclass
class BestOfReport:
    feature: str
    k: int
    succeeded: bool
    candidates: list[BestOfCandidate] = field(default_factory=list)
    winner_index: int | None = None
    proposal_id: str | None = None
    staged_paths: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def review(self) -> ReviewVerdict | None:
        """Winner's review — lets auto-settle apply the same hard conditions."""
        if self.winner_index is None:
            return None
        for c in self.candidates:
            if c.index == self.winner_index:
                return c.review
        return None

    def summary(self) -> str:
        lines = [f"best-of-{self.k}: {self.feature}", f"succeeded: {self.succeeded}"]
        if self.error:
            lines.append(f"error: {self.error}")
        for c in self.candidates:
            mark = " ← WINNER" if c.index == self.winner_index else ""
            rev = "-" if c.review is None else ("APPROVED" if c.review.approved else "rejected")
            lines.append(
                f"  candidate {c.index + 1}: {c.verdict} gate={'PASS' if c.gate_passed else 'fail'} "
                f"review={rev} files={c.files} diff={c.diff_bytes}B{mark}"
            )
        if self.proposal_id:
            lines += [
                "",
                f"winner staged: {PROPOSALS_DIR}/{self.proposal_id} ({len(self.staged_paths)} file(s))",
                f"  shepherd-dev settle-par {self.proposal_id} --repo <repo>            # accept",
                f"  shepherd-dev settle-par {self.proposal_id} --repo <repo> --reject   # discard",
            ]
        return "\n".join(lines)


def develop_best_of(
    repo_root: Path,
    feature: str,
    *,
    k: int,
    test_cmd: str,
    provider: str = "claude",
    placement: str = "jail",
    policy: ChangesetPolicy | None = None,
    max_attempts: int = 1,
    gate_timeout: int = 600,
    review_task=None,
    worker_task=None,
    worker_extra_args: list[dict | None] | None = None,
    context_pack: str | None = None,
) -> BestOfReport:
    """K parallel candidates from the same state; winner staged for settlement."""
    assert 2 <= k <= len(EMPHASES), f"k must be 2..{len(EMPHASES)}"
    policy = policy or ChangesetPolicy()
    report = BestOfReport(feature=feature, k=k, succeeded=False)
    task = worker_task or implement
    extras = worker_extra_args or [None] * k
    clones: list[Path] = []

    try:
        clones = [_clone_workspace(repo_root) for _ in range(k)]
        with ThreadPoolExecutor(max_workers=k) as pool:
            futures = [
                pool.submit(
                    _run_worker,
                    clones[i],
                    feature,
                    EMPHASES[i],
                    task=task,
                    extra_args=extras[i],
                    provider=provider,
                    placement=placement,
                    policy=policy,
                    max_attempts=max_attempts,
                    context_pack=context_pack,
                )
                for i in range(k)
            ]
            workers = [f.result() for f in futures]

        entries_by_idx: dict[int, dict[str, bytes]] = {}
        for i, w in enumerate(workers):
            if not (w.succeeded and w.entries):
                verdict = w.attempts[-1].verdict if w.attempts else "did not run"
                report.candidates.append(
                    BestOfCandidate(i, False, w.final_run_ref, 0, 0, False, None, verdict)
                )
                continue
            entries_by_idx[i] = w.entries
            gate = _run_gate(repo_root, w.entries, test_cmd, gate_timeout)
            review = None
            if gate.passed and review_task is not None:
                with sp.open(clones[i]) as workspace:
                    review = run_review(
                        workspace,
                        review_task,
                        feature=feature,
                        diff_text=_entries_diff_text(w.entries),
                        provider=provider,
                        placement=placement,
                        context_pack=context_pack,
                    )
            report.candidates.append(
                BestOfCandidate(
                    i,
                    True,
                    w.final_run_ref,
                    len(w.entries),
                    sum(len(v) for v in w.entries.values()),
                    gate.passed,
                    review,
                    "passed" if gate.passed else "gate_failed",
                )
            )

        # Deterministic ranking: gate first, then review approval, fewer review
        # issues, fewer files, smaller diff. Candidate order breaks ties.
        def rank_key(c: BestOfCandidate):
            approved = c.review.approved if (c.review and not c.review.error) else False
            issues = len(c.review.issues) if (c.review and not c.review.error) else 99
            return (not c.gate_passed, not approved, issues, c.files, c.diff_bytes, c.index)

        viable = [c for c in report.candidates if c.gate_passed]
        if not viable:
            report.error = "no candidate passed the gate"
            return report
        winner = min(viable, key=rank_key)
        report.winner_index = winner.index

        report.proposal_id, report.staged_paths = _stage_proposal(
            repo_root,
            entries_by_idx[winner.index],
            {
                "best_of": {
                    "feature": feature,
                    "k": k,
                    "winner": winner.index,
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
                },
                "review": _review_manifest(winner.review),
            },
        )
        report.succeeded = True
        return report
    finally:
        for clone in clones:
            shutil.rmtree(clone.parent, ignore_errors=True)
