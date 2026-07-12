"""Run history store: one JSONL event per invocation/settlement.

Foundation for auditing (auto-settle) and for CRO-lite (`shepherd-dev
optimize`), which mines failure modes and replays fix/guard cases. Writes are
best-effort and NEVER block or fail the main flow.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

HISTORY_DIR = Path(os.environ.get("SHEPHERD_DEV_HISTORY_DIR", "")) if os.environ.get(
    "SHEPHERD_DEV_HISTORY_DIR"
) else Path.home() / ".shepherd-dev" / "history"
RUNS_FILE = "runs.jsonl"

GATE_TAIL_LIMIT = 2000


def _git_sha(repo_root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, timeout=10
        )
        return proc.stdout.strip() or None
    except Exception:
        return None


def record_event(kind: str, payload: dict) -> None:
    """Append one event; swallow every error (history must never break a run)."""
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, **payload}
        with open(HISTORY_DIR / RUNS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        _gbrain_capture(kind, event)
    except Exception:
        pass


def load_events(kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Read the full history (optionally filtered by kind). Tolerant to bad lines."""
    events: list[dict] = []
    path = HISTORY_DIR / RUNS_FILE
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if kinds is None or event.get("kind") in kinds:
            events.append(event)
    return events


def run_payload(report, repo_root: Path, *, mode: str, test_cmd: str | None, provider: str, flags: dict) -> dict:
    """Serialize a DevReport for the store."""
    return {
        "feature": report.feature,
        "repo": str(repo_root),
        "sha": _git_sha(repo_root),
        "succeeded": report.succeeded,
        "final_run_ref": report.final_run_ref,
        "mode": mode,
        "test_cmd": test_cmd,
        "provider": provider,
        "flags": flags,
        "attempts": [
            {
                "n": a.number,
                "run_ref": a.run_ref,
                "verdict": a.verdict,
                "changed_paths": a.changed_paths,
                "policy_violations": a.policy_violations,
                "error": a.error,
                "gate_exit": a.gate.exit_code if a.gate else None,
                "gate_tail": (a.gate.output_tail[-GATE_TAIL_LIMIT:] if a.gate else None),
                "gate_infra_error": (a.gate.infra_error if a.gate else None),
            }
            for a in report.attempts
        ],
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
    }


def parallel_payload(report, repo_root: Path, *, test_cmd: str, provider: str, flags: dict) -> dict:
    """Serialize a ParallelReport for the store."""
    return {
        "features": report.features,
        "repo": str(repo_root),
        "sha": _git_sha(repo_root),
        "succeeded": report.succeeded,
        "proposal_id": report.proposal_id,
        "conflicts": report.conflicts,
        "handoff_used": report.handoff_used,
        "repairs": report.repairs,
        "test_cmd": test_cmd,
        "provider": provider,
        "flags": flags,
        "error": report.error,
        "combined_gate_exit": (report.combined_gate.exit_code if report.combined_gate else None),
        "workers": [
            {"succeeded": w.succeeded, "final_run_ref": w.final_run_ref, "attempts": len(w.attempts)}
            for w in report.workers
        ],
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
    }


def _gbrain_capture(kind: str, event: dict) -> None:
    """Mirror the event into GBrain when its CLI is present. Best-effort."""
    gbrain = shutil.which("gbrain")
    if not gbrain:
        return
    ref = event.get("final_run_ref") or event.get("proposal_id") or event.get("ref") or str(int(time.time()))
    slug = f"shepherd_dev_{kind}_{str(ref).replace('run-', '')}"
    body = (
        f"---\ntitle: shepherd-dev {kind} {ref}\ntags: [tipo:reference, projeto:shepherd]\n---\n\n"
        f"```json\n{json.dumps(event, ensure_ascii=False, indent=2)}\n```\n"
    )
    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(body)
            tmp = fh.name
        subprocess.run(
            [gbrain, "capture", "--file", tmp, "--slug", slug],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
