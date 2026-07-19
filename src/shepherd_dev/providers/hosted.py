"""Generic L1 hosted worker loop: isolate → execute CLI agent → policy → gate → stage.

Provider-agnostic extraction of the original Grok host path. Both the Grok and
Codex providers delegate here; the only provider-specific pieces are the
executor (how the CLI agent is invoked on the clone) and an optional LLM
review function. Does NOT import or call Claude / shepherd-ai. Settlement uses
the same `.shepherd-proposals/` stage as run2/best-of (`settle-par`).
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from ..diffcollect import DEFAULT_IGNORE_DIRS, collect_changed_entries
from ..policy import ChangesetPolicy, check_paths
from ..staging import PROPOSALS_DIR, stage_proposal
from ..supervisor import (
    IGNORED_DIRS,
    Attempt,
    DevReport,
    GateResult,
    ReviewVerdict,
    _format_guidance,
    _prior_attempt_guidance,
    _run_gate,
)


@dataclass
class ExecResult:
    ok: bool
    error: str | None = None
    duration_s: float | None = None
    output_tail: str = ""


class HostedExecutor(Protocol):
    def run(self, clone: Path, prompt: str, *, budget_seconds: int) -> ExecResult: ...


# Review function: (clone_with_changes, entries, feature) → ReviewVerdict.
ReviewFn = Callable[[Path, dict[str, bytes], str], ReviewVerdict]


@dataclass
class HostedReport:
    """L1 report: DevReport fields + staged proposal_id for settle-par."""

    feature: str
    succeeded: bool
    attempts: list[Attempt] = field(default_factory=list)
    review: ReviewVerdict | None = None
    repo: str = ""
    entries: dict[str, bytes] | None = None
    proposal_id: str | None = None
    staged_paths: list[str] = field(default_factory=list)
    backend: str = "host"
    provider: str = "hosted"
    error: str | None = None

    def as_dev_report(self) -> DevReport:
        """Project onto DevReport for history/memory helpers."""
        r = DevReport(
            feature=self.feature,
            succeeded=self.succeeded,
            attempts=list(self.attempts),
            final_run_ref=None,
            review=self.review,
            repo=self.repo,
            entries=self.entries,
        )
        if self.proposal_id:
            r.settlement_hint = (
                f"staged proposal {self.proposal_id} — "
                f"shepherd-dev settle-par {self.proposal_id} --repo {self.repo}"
            )
        return r

    def summary(self) -> str:
        lines = [
            f"feature: {self.feature}",
            f"succeeded: {self.succeeded}",
            f"provider: {self.provider} (backend={self.backend})",
        ]
        if self.error:
            lines.append(f"error: {self.error}")
        for a in self.attempts:
            lines.append(
                f"  attempt {a.number}: run={a.run_ref} verdict={a.verdict} "
                f"changed={len(a.changed_paths)}"
            )
            if a.error:
                lines.append(f"    error: {a.error}")
            if a.policy_violations:
                lines += [f"    policy: {v}" for v in a.policy_violations]
            if a.gate and not a.gate.passed:
                reason = a.gate.infra_error or a.gate.output_tail[-500:]
                lines.append(f"    gate: exit={a.gate.exit_code} {reason}")
        if self.review:
            if self.review.error:
                lines.append(f"review: UNAVAILABLE ({self.review.error})")
            else:
                lines.append(
                    f"review: {'APPROVED' if self.review.approved else 'REJECTED'} — {self.review.summary}"
                )
                lines += [f"  issue: {i}" for i in self.review.issues]
        if self.proposal_id:
            repo_arg = f" --repo {self.repo}" if self.repo else ""
            lines += [
                "",
                f"staged for human settlement ({PROPOSALS_DIR}/{self.proposal_id}, "
                f"{len(self.staged_paths)} file(s)):",
                f"  shepherd-dev settle-par {self.proposal_id}{repo_arg}            # accept",
                f"  shepherd-dev settle-par {self.proposal_id}{repo_arg} --reject   # discard",
            ]
        return "\n".join(lines)


def clone_repo(repo_root: Path, *, prefix: str = "shepherd-hosted-") -> Path:
    dest = Path(tempfile.mkdtemp(prefix=prefix))
    clone = dest / "repo"
    ignore = set(IGNORED_DIRS) | DEFAULT_IGNORE_DIRS | {".git"}
    shutil.copytree(
        repo_root,
        clone,
        ignore=shutil.ignore_patterns(*ignore),
        symlinks=True,
    )
    return clone


def worker_prompt(
    feature: str,
    *,
    guidance: str,
    context_pack: str | None,
    mode: str,
) -> str:
    role = (
        "Implement the requested feature in this repository."
        if mode == "feature"
        else "Write or update automated tests for the described feature. Do not change production code."
    )
    parts = [
        role,
        "",
        "Requirements:",
        "- Follow existing conventions (style, naming, layout, test framework).",
        "- Touch only files needed for this request. No drive-by refactors.",
        "- Keep the change minimal and complete: no TODOs, no placeholders.",
        "- Write real files into the working tree (this directory IS the repo clone).",
        "",
        f"Feature request:\n{feature}",
    ]
    if context_pack:
        parts += ["", "Context pack (prefer this over blind exploration):", context_pack]
    if guidance:
        parts += ["", guidance]
    return "\n".join(parts)


def heuristic_review(entries: dict[str, bytes], feature: str) -> ReviewVerdict:
    """Deterministic lightweight review when no LLM review is requested/available.

    Flags empty proposals and oversized diffs; otherwise a weak advisory signal
    — auto-settle still requires a real reviewing provider.
    """
    if not entries:
        return ReviewVerdict(False, "no files in proposal", ["empty proposal"])
    n = len(entries)
    size = sum(len(v) for v in entries.values())
    issues: list[str] = []
    if n > 30:
        issues.append(f"touches many files ({n})")
    if size > 200_000:
        issues.append(f"large diff ({size} bytes)")
    ok = not issues
    return ReviewVerdict(
        approved=ok,
        summary=(
            f"heuristic review of {n} file(s) for {feature!r}: "
            + ("looks bounded" if ok else "needs human attention")
        ),
        issues=issues,
    )


def develop_hosted(
    repo_root: Path,
    feature: str,
    *,
    provider: str,
    executor: HostedExecutor,
    test_cmd: str | None,
    max_attempts: int = 3,
    gate_timeout: int = 600,
    worker_budget: int = 900,
    policy: ChangesetPolicy | None = None,
    context_pack: str | None = None,
    mode: str = "feature",
    do_review: bool = False,
    review_fn: ReviewFn | None = None,
    backend: str = "host",
    reporter=None,
) -> HostedReport:
    """Supervised CLI-agent loop (L1 host). Never mutates repo_root; stages on success.

    review_fn, when given and do_review is True, runs on the live clone (changes
    applied) so LLM reviewers can inspect the modified tree; falls back to the
    deterministic heuristic when absent.
    """
    from ..progress import NullProgress

    reporter = reporter or NullProgress()
    policy = policy or ChangesetPolicy()
    report = HostedReport(
        feature=feature, succeeded=False, repo=str(repo_root),
        backend=backend, provider=provider,
    )
    guidance = ""

    for number in range(1, max_attempts + 1):
        reporter.step(f"attempt {number}/{max_attempts} · {provider} worker ({backend})")
        clone: Path | None = None
        try:
            clone = clone_repo(repo_root, prefix=f"shepherd-{provider}-")
            prompt = worker_prompt(feature, guidance=guidance, context_pack=context_pack, mode=mode)
            result: ExecResult = executor.run(clone, prompt, budget_seconds=worker_budget)
            if not result.ok:
                reporter.fail(result.error or f"{provider} worker failed")
                report.attempts.append(
                    Attempt(
                        number, f"{provider}-{number}", [], [], None, "run_failed",
                        error=result.error, duration_s=result.duration_s,
                    )
                )
                guidance = (
                    "PREVIOUS ATTEMPT: the worker run failed "
                    f"({result.error}). Be more direct; make the minimal change."
                )
                continue

            entries = collect_changed_entries(repo_root, clone)
            changed = list(entries)
            reporter.note(f"worker: {len(changed)} file(s)" + (f": {', '.join(changed[:8])}" if changed else ""))

            if not changed:
                reporter.fail("no file changes")
                report.attempts.append(
                    Attempt(number, f"{provider}-{number}", [], [], None, "no_change", duration_s=result.duration_s)
                )
                guidance = (
                    "PREVIOUS ATTEMPT: you produced no file changes. Implement the feature "
                    "by writing files into the repository now."
                )
                continue

            verdict = check_paths(changed, policy)
            if not verdict.ok:
                reporter.fail(f"policy: {len(verdict.violations)} violation(s)")
                report.attempts.append(
                    Attempt(
                        number, f"{provider}-{number}", changed, verdict.violations, None,
                        "policy_rejected", duration_s=result.duration_s,
                    )
                )
                guidance = _prior_attempt_guidance(entries) + _format_guidance(
                    "policy", violations=verdict.violations
                )
                continue

            gate: GateResult | None = None
            if test_cmd is not None:
                reporter.step(f"attempt {number} · gate")
                gate = _run_gate(repo_root, entries, test_cmd, gate_timeout)
                if gate.infra_error:
                    reporter.fail(f"gate infra: {gate.infra_error[:80]}")
                    report.attempts.append(
                        Attempt(number, f"{provider}-{number}", changed, [], gate, "tests_failed", duration_s=result.duration_s)
                    )
                    report.error = gate.infra_error
                    return report
                if not gate.passed:
                    reporter.fail(f"gate failed (exit {gate.exit_code})")
                    report.attempts.append(
                        Attempt(number, f"{provider}-{number}", changed, [], gate, "tests_failed", duration_s=result.duration_s)
                    )
                    guidance = _prior_attempt_guidance(entries) + _format_guidance("gate", gate=gate)
                    continue

            report.attempts.append(
                Attempt(number, f"{provider}-{number}", changed, [], gate, "passed", duration_s=result.duration_s)
            )
            report.entries = entries
            if do_review:
                reporter.step(f"attempt {number} · review")
                if review_fn is not None:
                    report.review = review_fn(clone, entries, feature)
                else:
                    report.review = heuristic_review(entries, feature)

            report.proposal_id, report.staged_paths = stage_proposal(
                repo_root,
                entries,
                {
                    "provider": provider,
                    "backend": backend,
                    "feature": feature,
                    "mode": mode,
                    "gate": (
                        None
                        if gate is None
                        else {"passed": gate.passed, "exit_code": gate.exit_code}
                    ),
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
                },
            )
            report.succeeded = True
            return report
        finally:
            if clone is not None:
                shutil.rmtree(clone.parent, ignore_errors=True)

    if not report.succeeded and not report.error:
        report.error = "all attempts exhausted without a passing proposal"
    return report
