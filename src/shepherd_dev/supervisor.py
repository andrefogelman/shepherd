"""Supervisor meta-agent: runs a worker task in a sandbox, applies the
changeset policy, gates the retained output on the repo's test suite, and
retries with injected guidance on failure.

The supervisor NEVER applies anything to the workspace. A passing attempt
stays retained; settlement (select/apply/discard) is always a human decision.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .policy import ChangesetPolicy, check_paths

IGNORED_DIRS = {
    ".vcscore",
    ".venv",
    "node_modules",
    "__pycache__",
    ".shepherd",
    ".review",
    ".shepherd-proposals",
}
DIFF_TEXT_LIMIT = 60_000  # chars of proposal content handed to the reviewer


@dataclass
class GateResult:
    passed: bool
    exit_code: int | None
    output_tail: str
    infra_error: str | None = None  # suite could not run at all


@dataclass
class Attempt:
    number: int
    run_ref: str
    changed_paths: list[str]
    policy_violations: list[str]
    gate: GateResult | None
    verdict: str  # "run_failed" | "no_change" | "policy_rejected" | "tests_failed" | "passed"
    error: str | None = None
    duration_s: float | None = None  # worker wall-clock (cost/speed telemetry)


@dataclass
class ReviewVerdict:
    approved: bool
    summary: str
    issues: list[str] = field(default_factory=list)
    error: str | None = None  # review ran but verdict could not be obtained


@dataclass
class DevReport:
    feature: str
    succeeded: bool
    attempts: list[Attempt] = field(default_factory=list)
    final_run_ref: str | None = None
    settlement_hint: str | None = None
    review: ReviewVerdict | None = None
    repo: str = ""
    # content entries of the passing proposal (set on success) — consumed by
    # the parallel coordinator; per-file bytes, small by policy cap
    entries: dict[str, bytes] | None = None

    def summary(self) -> str:
        lines = [f"feature: {self.feature}", f"succeeded: {self.succeeded}"]
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
                lines.append(f"review: {'APPROVED' if self.review.approved else 'REJECTED'} — {self.review.summary}")
                lines += [f"  issue: {i}" for i in self.review.issues]
        if self.final_run_ref:
            repo_arg = f" --repo {self.repo}" if self.repo else ""
            lines += [
                "",
                "retained for human settlement:",
                f"  shepherd run changeset {self.final_run_ref}                # inspect",
                f"  shepherd-dev settle {self.final_run_ref}{repo_arg}           # accept: advance world + write files",
                f"  shepherd-dev settle {self.final_run_ref}{repo_arg} --reject  # discard proposal",
            ]
        return "\n".join(lines)


def materialize_into(root: Path, entries: dict[str, bytes]) -> list[str]:
    """Write changeset content entries under root.

    Refuses paths that escape root. Returns the list of written paths.
    """
    written: list[str] = []
    resolved_root = root.resolve()
    for rel, content in entries.items():
        target = (root / rel).resolve()
        if not target.is_relative_to(resolved_root):
            raise ValueError(f"changeset path escapes repo root: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written.append(rel)
    return written


def read_changeset_entries(changeset) -> dict[str, bytes]:
    """Snapshot a retained changeset's content entries into memory.

    v0.3.0 lane reality (verified on 3 workspaces): runs fork from the
    workspace's ORIGINAL adoption basis, not from later settlements, so
    changed_paths can list stale-basis artifacts whose content is
    unavailable (read_file -> None). Those are NOT worker actions; the
    git worktree is our source of truth, so they are skipped. Consequence:
    genuine worker deletions cannot be expressed in this lane (documented
    limitation; effect-stream support in F3).
    """
    entries: dict[str, bytes] = {}
    for rel in changeset.changed_paths:
        entry = changeset.read_file(rel)  # (bytes, mode) | None
        if entry is not None:
            entries[rel] = entry[0]
    return entries


def _materialize(repo_root: Path, entries: dict[str, bytes], dest: Path) -> None:
    """Copy the repo and overlay the proposal's content entries on top."""
    shutil.copytree(
        repo_root,
        dest,
        ignore=shutil.ignore_patterns(*IGNORED_DIRS),
        dirs_exist_ok=True,
        symlinks=True,
    )
    materialize_into(dest, entries)


def _format_guidance(kind: str, *, violations: list[str] | None = None, gate: GateResult | None = None) -> str:
    """Structured feedback injected into the worker's next attempt.

    Templates live in prompts.py (CRO-lite surface); {TOKENS} are substituted
    with str.replace — gate tails may contain braces, so never str.format.
    """
    from .tasks import get_prompt

    if kind == "policy":
        return get_prompt("guidance_policy").replace("{VIOLATIONS}", "\n- ".join(violations or []))
    if kind == "gate":
        assert gate is not None
        return (
            get_prompt("guidance_gate")
            .replace("{EXIT}", str(gate.exit_code))
            .replace("{TAIL}", gate.output_tail[-2000:])
        )
    raise ValueError(f"unknown guidance kind: {kind}")


def set_worker_budget(seconds: int) -> None:
    """Raise the Claude workspace provider's wall-clock budget.

    Alpha workaround for shepherd-ai 0.3.0: `budget`/`timeout` are reserved
    runtime fields and ClaudeHeadlessProvider hardcodes budget_seconds=240,
    too little for real features. Rebinds the internal transport seam
    (private API — revisit on framework upgrade).
    """
    from shepherd_dialect import providers
    from shepherd_dialect.workspace_control import runtime_provider as rp

    def transport(invocation):
        return providers.ClaudeHeadlessProvider(
            provider_id=invocation.provider_id,
            prompt=invocation.prompt,
            model=invocation.model_name,
            budget_seconds=seconds,
        )

    rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = rp._WorkspaceRuntimeProviderTransports(
        claude=transport
    )


def build_diff_text(changeset, limit: int = DIFF_TEXT_LIMIT) -> str:
    """Render a retained changeset's content entries as reviewer-readable text."""
    parts: list[str] = []
    for rel, content in read_changeset_entries(changeset).items():
        text = content.decode("utf-8", errors="replace")
        parts.append(f"=== FILE: {rel} (proposed content) ===\n{text}")
    diff = "\n\n".join(parts)
    if len(diff) > limit:
        diff = diff[:limit] + f"\n\n[... truncated at {limit} chars ...]"
    return diff


def run_review(
    workspace,
    review_task,
    *,
    feature: str,
    changeset=None,
    diff_text: str | None = None,
    provider: str = "claude",
    placement: str = "jail",
    context_pack: str | None = None,
) -> ReviewVerdict:
    """Run the reviewer against a passing proposal.

    v0.2 lane limits (bindings need disjoint roots; multi-binding runs take no
    execution provider) rule out a syscall-read-only reviewer, so isolation is
    custody-based instead: the reviewer runs in the single-repo lane, its
    output is retained (never applied), a deterministic guard requires the
    changeset to be exactly {REVIEW.json}, and the output is always discarded
    after the verdict is read.
    """
    if diff_text is None:
        diff_text = build_diff_text(changeset)
    workspace.tasks.register(review_task)
    try:
        run = workspace.run(
            review_task,
            repo=workspace.git_repo(),
            placement=placement,
            runtime={"provider": provider},
            args={"feature": feature, "diff": diff_text, "context": context_pack or ""},
        )
    except Exception as exc:
        return ReviewVerdict(approved=False, summary="", error=f"review run failed: {exc}")

    output = run.output()
    try:
        entries = read_changeset_entries(output.changeset())
        touched = sorted(entries)
        if touched and touched != ["REVIEW.json"]:
            return ReviewVerdict(
                approved=False,
                summary="",
                error=f"reviewer touched files beyond REVIEW.json: {touched} — verdict invalidated",
            )
        if "REVIEW.json" not in entries:
            return ReviewVerdict(approved=False, summary="", error="reviewer produced no REVIEW.json")
        data = json.loads(entries["REVIEW.json"].decode("utf-8", errors="replace"))
    except Exception as exc:
        return ReviewVerdict(approved=False, summary="", error=f"invalid REVIEW.json: {exc}")
    finally:
        try:
            output.discard()
        except Exception:
            pass

    return ReviewVerdict(
        approved=bool(data.get("approved", False)),
        summary=str(data.get("summary", "")),
        issues=[str(i) for i in data.get("issues", [])],
    )


_TEST_FILE_RE = re.compile(r"(\.test\.(ts|tsx|js|jsx|mjs)|_test\.py|_test\.exs)$")


def _resolve_gate_cmd(test_cmd: str, entries: dict[str, bytes]) -> str | None:
    """Substitute {NEW_TESTS} with the proposal's own test files (native gate).

    Returns None when the placeholder is present but the proposal added no
    tests — the gate must then fail loudly instead of running nothing."""
    if "{NEW_TESTS}" not in test_cmd:
        return test_cmd
    import shlex

    new_tests = sorted(rel for rel in entries if _TEST_FILE_RE.search(rel))
    if not new_tests:
        return None
    return test_cmd.replace("{NEW_TESTS}", " ".join(shlex.quote(t) for t in new_tests))


def _run_gate(repo_root: Path, entries: dict[str, bytes], test_cmd: str, timeout: int) -> GateResult:
    """Run the repo's test suite against a materialized copy of the proposal."""
    resolved = _resolve_gate_cmd(test_cmd, entries)
    if resolved is None:
        return GateResult(
            False, 1,
            "native gate: the proposal contains no test files (*.test.* / *_test.*) — "
            "write tests for the feature; the gate runs exactly the tests you add.",
        )
    test_cmd = resolved
    with tempfile.TemporaryDirectory(prefix="shepherd-gate-") as tmp:
        staged = Path(tmp) / "staged"
        try:
            _materialize(repo_root, entries, staged)
        except Exception as exc:
            return GateResult(False, None, "", infra_error=f"materialize failed: {exc}")
        try:
            proc = subprocess.run(
                test_cmd,
                shell=True,
                cwd=staged,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return GateResult(False, None, "", infra_error=f"test suite timed out after {timeout}s")
        except OSError as exc:
            return GateResult(False, None, "", infra_error=f"could not run test suite: {exc}")
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-4000:]
        return GateResult(passed=proc.returncode == 0, exit_code=proc.returncode, output_tail=tail)


def develop(
    workspace,
    task,
    *,
    repo,
    repo_root: Path,
    feature: str,
    test_cmd: str | None,
    provider: str = "claude",
    placement: str = "jail",
    max_attempts: int = 3,
    gate_timeout: int = 600,
    policy: ChangesetPolicy | None = None,
    extra_args: dict | None = None,
    review_task=None,
    initial_guidance: str = "",
    context_pack: str | None = None,
) -> DevReport:
    """Supervised development loop. Returns a report; never mutates the workspace.

    test_cmd=None skips the test gate (policy-only pass) — used by the parallel
    coordinator, whose combined gate judges the merged proposal instead.
    initial_guidance seeds the first attempt (e.g. teammate/handoff context);
    later attempts replace it with concrete failure feedback. context_pack, when
    given, is prepended to the guidance of EVERY attempt (built once per command,
    reused across retries — the lane-honest analogue of prefix reuse).
    """
    import time as _time

    policy = policy or ChangesetPolicy()
    report = DevReport(feature=feature, succeeded=False, repo=str(repo_root))
    guidance = initial_guidance

    workspace.tasks.register(task)

    for number in range(1, max_attempts + 1):
        args = {"repo": repo, **(extra_args or {})}
        if "output_path" not in args:  # real worker takes feature/guidance
            args["feature"] = feature
            args["guidance"] = (
                f"{context_pack}\n\n{guidance}".strip() if context_pack else guidance
            )

        started = _time.monotonic()
        try:
            run = workspace.run(
                task,
                placement=placement,
                runtime={"provider": provider},
                **args,
            )
        except Exception as exc:
            report.attempts.append(
                Attempt(
                    number, "(no run)", [], [], None, "run_failed",
                    error=f"{type(exc).__name__}: {exc}",
                    duration_s=round(_time.monotonic() - started, 1),
                )
            )
            guidance = (
                "PREVIOUS ATTEMPT: the agent run itself failed "
                f"({type(exc).__name__}). Work efficiently and stay within the "
                "wall-clock budget: read only what you need, then write the change."
            )
            continue
        duration = round(_time.monotonic() - started, 1)
        output = run.output()
        changeset = output.changeset()
        entries = read_changeset_entries(changeset)
        changed = list(entries)

        if not changed:
            # Worker produced nothing: either it judged the feature already
            # satisfied in its world basis, or the agent run failed silently.
            output.discard()
            report.attempts.append(Attempt(number, run.run_ref, changed, [], None, "no_change", duration_s=duration))
            guidance = (
                "PREVIOUS ATTEMPT: you produced no file changes at all. "
                "If the feature genuinely already exists, say so by making the "
                "minimal change that proves it (e.g. a test); otherwise implement it now."
            )
            continue

        verdict_policy = check_paths(changed, policy)
        if not verdict_policy.ok:
            output.discard()
            report.attempts.append(
                Attempt(number, run.run_ref, changed, verdict_policy.violations, None, "policy_rejected", duration_s=duration)
            )
            guidance = _format_guidance("policy", violations=verdict_policy.violations)
            continue

        gate: GateResult | None = None
        if test_cmd is not None:
            gate = _run_gate(repo_root, entries, test_cmd, gate_timeout)
            if gate.infra_error:
                # Suite could not run: abort, do not burn attempts, keep output retained
                report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "tests_failed", duration_s=duration))
                report.settlement_hint = f"gate infra error: {gate.infra_error}"
                return report

            if not gate.passed:
                output.discard()
                report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "tests_failed", duration_s=duration))
                guidance = _format_guidance("gate", gate=gate)
                continue

        report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "passed", duration_s=duration))
        report.succeeded = True
        report.final_run_ref = run.run_ref
        report.entries = entries
        if review_task is not None:
            report.review = run_review(
                workspace,
                review_task,
                feature=feature,
                changeset=changeset,
                provider=provider,
                placement=placement,
                context_pack=context_pack,
            )
        return report

    return report
