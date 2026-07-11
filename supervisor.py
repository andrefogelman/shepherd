"""Supervisor meta-agent: runs a worker task in a sandbox, applies the
changeset policy, gates the retained output on the repo's test suite, and
retries with injected guidance on failure.

The supervisor NEVER applies anything to the workspace. A passing attempt
stays retained; settlement (select/apply/discard) is always a human decision.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from policy import ChangesetPolicy, check_changeset

IGNORED_DIRS = {".vcscore", ".venv", "node_modules", "__pycache__", ".shepherd"}


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
    verdict: str  # "policy_rejected" | "tests_failed" | "passed"


@dataclass
class DevReport:
    feature: str
    succeeded: bool
    attempts: list[Attempt] = field(default_factory=list)
    final_run_ref: str | None = None
    settlement_hint: str | None = None

    def summary(self) -> str:
        lines = [f"feature: {self.feature}", f"succeeded: {self.succeeded}"]
        for a in self.attempts:
            lines.append(
                f"  attempt {a.number}: run={a.run_ref} verdict={a.verdict} "
                f"changed={len(a.changed_paths)}"
            )
            if a.policy_violations:
                lines += [f"    policy: {v}" for v in a.policy_violations]
            if a.gate and not a.gate.passed:
                reason = a.gate.infra_error or a.gate.output_tail[-500:]
                lines.append(f"    gate: exit={a.gate.exit_code} {reason}")
        if self.final_run_ref:
            lines += [
                "",
                "retained for human settlement:",
                f"  shepherd run changeset {self.final_run_ref}",
                f"  shepherd run select {self.final_run_ref}   # keep",
                f"  shepherd run apply {self.final_run_ref}    # merge onto moved-on workspace",
                f"  shepherd run discard {self.final_run_ref}  # reject",
            ]
        return "\n".join(lines)


def _materialize(repo_root: Path, changeset, dest: Path) -> None:
    """Copy the repo and overlay the retained changeset on top."""
    shutil.copytree(
        repo_root,
        dest,
        ignore=shutil.ignore_patterns(*IGNORED_DIRS),
        dirs_exist_ok=True,
        symlinks=True,
    )
    for rel in changeset.changed_paths:
        target = dest / rel
        entry = changeset.read_file(rel)  # (bytes, mode) | None
        if entry is None:
            target.unlink(missing_ok=True)
            continue
        content, _mode = entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _run_gate(repo_root: Path, changeset, test_cmd: str, timeout: int) -> GateResult:
    """Run the repo's test suite against a materialized copy of the proposal."""
    with tempfile.TemporaryDirectory(prefix="shepherd-gate-") as tmp:
        staged = Path(tmp) / "staged"
        try:
            _materialize(repo_root, changeset, staged)
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
    test_cmd: str,
    provider: str = "claude",
    placement: str = "jail",
    max_attempts: int = 3,
    gate_timeout: int = 600,
    policy: ChangesetPolicy | None = None,
    extra_args: dict | None = None,
) -> DevReport:
    """Supervised development loop. Returns a report; never mutates the workspace."""
    policy = policy or ChangesetPolicy()
    report = DevReport(feature=feature, succeeded=False)
    guidance = ""

    workspace.tasks.register(task)

    for number in range(1, max_attempts + 1):
        args = {"repo": repo, **(extra_args or {})}
        if "output_path" not in args:  # real worker takes feature/guidance
            args["feature"] = feature
            args["guidance"] = guidance

        run = workspace.run(
            task,
            placement=placement,
            runtime={"provider": provider},
            **args,
        )
        output = run.output()
        changeset = output.changeset()
        changed = list(changeset.changed_paths)

        verdict_policy = check_changeset(changeset, policy)
        if not verdict_policy.ok:
            output.discard()
            report.attempts.append(
                Attempt(number, run.run_ref, changed, verdict_policy.violations, None, "policy_rejected")
            )
            guidance = (
                "Your previous attempt was rejected by policy before testing:\n- "
                + "\n- ".join(verdict_policy.violations)
                + "\nStay within scope and try again."
            )
            continue

        gate = _run_gate(repo_root, changeset, test_cmd, gate_timeout)
        if gate.infra_error:
            # Suite could not run: abort, do not burn attempts, keep output retained
            report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "tests_failed"))
            report.settlement_hint = f"gate infra error: {gate.infra_error}"
            return report

        if not gate.passed:
            output.discard()
            report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "tests_failed"))
            guidance = (
                f"Your previous attempt failed the test suite (exit {gate.exit_code}).\n"
                f"Test output (tail):\n{gate.output_tail[-2000:]}\n"
                "Fix the root cause and try again."
            )
            continue

        report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "passed"))
        report.succeeded = True
        report.final_run_ref = run.run_ref
        return report

    return report
