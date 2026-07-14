"""CRO-lite: mine the run history for failure modes, ask a meta-optimizer to
propose a prompt edit, and validate it by replaying a fix set (past failures,
must improve) and a guard set (past passes, must not regress) with the candidate
prompt. Accept only edits that strictly help.

This is the shepherd-ai 0.3.0-honest version of the paper's Counterfactual
Replay Optimization: the public workspace lane has no cheap byte-identical
suffix replay, so we re-run whole cases (pinned to their original SHAs) in
subprocesses with the candidate prompt injected via the overrides file. Cost is
real tokens; keep fix/guard sets small.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import history
from .tasks import DEFAULT_PROMPTS, load_overrides, save_overrides

EDITABLE_KEYS = ("implement", "write_tests", "review", "guidance_policy", "guidance_gate")


@dataclass
class Candidate:
    key: str
    text: str
    rationale: str


@dataclass
class ReplayCase:
    feature: str
    repo: str
    sha: str | None
    test_cmd: str
    mode: str
    was_success: bool  # outcome under the CURRENT prompt (from history)


@dataclass
class OptimizeReport:
    considered: int = 0
    candidate: Candidate | None = None
    fix_before: int = 0
    fix_after: int = 0
    guard_before: int = 0
    guard_after: int = 0
    accepted: bool = False
    reason: str = ""
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"optimize: analyzed {self.considered} historical runs"]
        if self.candidate is None:
            lines.append(f"no candidate: {self.reason}")
            return "\n".join(lines)
        lines += [
            f"candidate edit to prompt '{self.candidate.key}': {self.candidate.rationale}",
            f"  fix set  (past failures): {self.fix_before} -> {self.fix_after} passing",
            f"  guard set (past passes) : {self.guard_before} -> {self.guard_after} passing",
            f"decision: {'ACCEPTED' if self.accepted else 'REJECTED'} — {self.reason}",
        ]
        if self.accepted:
            lines.append("prompt override saved; future runs use it (revert: edit ~/.shepherd-dev/prompts-overrides.json)")
        for e in self.errors:
            lines.append(f"  ! {e}")
        return "\n".join(lines)


def _select_cases(fix_n: int, guard_n: int) -> tuple[list[ReplayCase], list[ReplayCase]]:
    """Fix set = most recent real failures; guard set = most recent successes.
    Only 'run' events with a real test_cmd, provider claude, and a pinned sha."""
    fixes: list[ReplayCase] = []
    guards: list[ReplayCase] = []
    for ev in reversed(history.load_events(("run",))):
        if ev.get("provider") != "claude" or not ev.get("test_cmd") or not ev.get("sha"):
            continue
        case = ReplayCase(
            feature=ev["feature"], repo=ev["repo"], sha=ev["sha"],
            test_cmd=ev["test_cmd"], mode=ev.get("mode", "feature"),
            was_success=bool(ev.get("succeeded")),
        )
        if not case.was_success and len(fixes) < fix_n:
            fixes.append(case)
        elif case.was_success and len(guards) < guard_n:
            guards.append(case)
        if len(fixes) >= fix_n and len(guards) >= guard_n:
            break
    return fixes, guards


def _failure_digest(max_events: int = 40) -> str:
    """Compact evidence of failure modes for the meta-optimizer."""
    lines: list[str] = []
    seen = 0
    for ev in reversed(history.load_events(("run",))):
        if ev.get("succeeded"):
            continue
        atts = ev.get("attempts", [])
        last = atts[-1] if atts else {}
        lines.append(
            f"- feature: {ev.get('feature','')[:120]}\n"
            f"  verdict: {last.get('verdict')}  gate_exit: {last.get('gate_exit')}\n"
            f"  gate_tail: {(last.get('gate_tail') or '')[-400:]}\n"
            f"  review_issues: {(ev.get('review') or {}).get('issues')}"
        )
        seen += 1
        if seen >= max_events:
            break
    return "\n".join(lines) if lines else "(no failures in history)"


def _propose(model: str) -> Candidate | None:
    """Ask Claude (headless) for ONE prompt edit as strict JSON."""
    import shutil

    claude = shutil.which("claude")
    if not claude:
        return None
    current = {k: DEFAULT_PROMPTS[k] for k in EDITABLE_KEYS}
    current.update({k: v for k, v in load_overrides().items() if k in EDITABLE_KEYS})
    prompt = (
        "You tune the prompts of an automated coding agent. Below are the CURRENT prompts "
        "and a digest of recent FAILED runs (test-gate failures and reviewer issues).\n\n"
        "Propose exactly ONE edit to ONE prompt that would most likely fix a recurring "
        "failure mode WITHOUT causing regressions. Keep the same intent and any {TOKENS} "
        "(guidance_gate uses {EXIT} and {TAIL}; guidance_policy uses {VIOLATIONS}). "
        "Return ONLY minified JSON: {\"key\":\"<one of "
        + "|".join(EDITABLE_KEYS) + ">\",\"text\":\"<full replacement prompt>\","
        "\"rationale\":\"<one sentence>\"}\n\n"
        "CURRENT PROMPTS:\n" + json.dumps(current, ensure_ascii=False, indent=2)
        + "\n\nRECENT FAILURES:\n" + _failure_digest()
    )
    try:
        proc = subprocess.run(
            [claude, "--bare", "-p", prompt, "--model", model],
            capture_output=True, text=True, timeout=180,
        )
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    start, end = out.find("{"), out.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(out[start : end + 1])
    except Exception:
        return None
    key, text = data.get("key"), data.get("text")
    if key not in EDITABLE_KEYS or not isinstance(text, str) or not text.strip():
        return None
    if key.startswith("guidance_"):
        need = ("{EXIT}", "{TAIL}") if key == "guidance_gate" else ("{VIOLATIONS}",)
        if not all(tok in text for tok in need):
            return None
    return Candidate(key=key, text=text, rationale=str(data.get("rationale", "")))


def _replay(case: ReplayCase, overrides_path: str | None, worker_budget: int) -> bool:
    """Re-run one case at its pinned SHA in a subprocess; True = gate passed.

    The candidate prompt (if any) is injected via SHEPHERD_DEV_PROMPTS_OVERRIDES.
    A temp git worktree pinned to the sha keeps the run isolated and reproducible.
    """
    repo = Path(case.repo)
    if not repo.is_dir() or not case.sha:
        return False
    with tempfile.TemporaryDirectory(prefix="shepherd-cro-") as tmp:
        wt = Path(tmp) / "wt"
        add = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), case.sha],
            capture_output=True, text=True,
        )
        if add.returncode != 0:
            return False
        try:
            env = dict(os.environ)
            if overrides_path:
                env["SHEPHERD_DEV_PROMPTS_OVERRIDES"] = overrides_path
            # history off during replay so validation runs don't pollute the store
            env["SHEPHERD_DEV_HISTORY_DIR"] = str(Path(tmp) / "nohist")
            # a fresh worktree is not yet a Shepherd workspace
            init = subprocess.run(
                [sys.executable, "-m", "shepherd_dev.cli", "init", "--repo", str(wt)],
                capture_output=True, text=True, env=env, timeout=120,
            )
            if init.returncode != 0:
                return False
            proc = subprocess.run(
                [
                    sys.executable, "-m", "shepherd_dev.cli", "run", case.feature,
                    "--repo", str(wt), "--test-cmd", case.test_cmd,
                    "--mode", case.mode, "--no-review", "--max-attempts", "1",
                    "--worker-budget", str(worker_budget),
                ],
                capture_output=True, text=True, env=env, timeout=worker_budget + 300,
            )
            # `run` exits 0 iff the gate passed (report.succeeded); use the exit
            # code, not a stdout substring, so replay isn't fragile to formatting (#16).
            return proc.returncode == 0
        except Exception:
            return False
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
            )


def optimize(
    *,
    fix_n: int = 3,
    guard_n: int = 3,
    model: str = "claude-opus-4-8",
    worker_budget: int = 900,
    apply: bool = False,
) -> OptimizeReport:
    report = OptimizeReport()
    all_runs = history.load_events(("run",))
    report.considered = len(all_runs)

    fixes, guards = _select_cases(fix_n, guard_n)
    if not fixes:
        report.reason = "no replayable past failures in history (need claude runs with a test_cmd and sha)"
        return report

    cand = _propose(model)
    if cand is None:
        report.candidate = None
        report.reason = "meta-optimizer produced no valid candidate"
        return report
    report.candidate = cand

    # Baselines under the CURRENT prompt come from history (no re-run needed).
    report.fix_before = 0  # by construction these are failures
    report.guard_before = len(guards)

    ovr = {**load_overrides(), cand.key: cand.text}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(ovr, fh, ensure_ascii=False)
        ovr_path = fh.name
    try:
        report.fix_after = sum(_replay(c, ovr_path, worker_budget) for c in fixes)
        report.guard_after = sum(_replay(c, ovr_path, worker_budget) for c in guards)
    finally:
        try:
            os.unlink(ovr_path)
        except Exception:
            pass

    improves = report.fix_after > report.fix_before
    regresses = report.guard_after < report.guard_before
    if improves and not regresses:
        report.accepted = True
        report.reason = f"fix set improved (+{report.fix_after}) with no guard regression"
        if apply:
            save_overrides({cand.key: cand.text})
        else:
            report.reason += " (dry-run; pass --apply to persist)"
            report.accepted = False
    elif not improves:
        report.reason = "candidate did not fix any failure case"
    else:
        report.reason = f"candidate regressed the guard set ({report.guard_before} -> {report.guard_after})"
    return report
