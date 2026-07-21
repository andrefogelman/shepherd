"""Machine-readable run status — shepherd's ground truth, exposed.

Mission-control tools around coding agents infer state by parsing terminal
output; shepherd doesn't have to guess — the per-run event log IS the state.
``runs_status`` derives, per recorded run: finished (succeeded/failed from
run.summary), running (recent events, current phase/attempt), or stale (no
summary and no recent activity — likely killed). The ``status`` CLI command
renders it for humans or as JSON for any external UI.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .events import load_run_events

#: A run with no summary and no event for this long is presumed dead.
STALE_AFTER_SECONDS = 30 * 60

_LANE_SUFFIX_RE = re.compile(r"-(w\d+|c\d+|wa|wb)$")


def runs_status(root: Path | None = None, limit: int = 10) -> list[dict]:
    """Status rows for the most recent runs, newest first."""
    from .events import _default_runs_root

    base = Path(root) if root else _default_runs_root()
    try:
        run_ids = sorted((p.name for p in base.iterdir() if p.is_dir()), reverse=True)
    except Exception:
        return []
    rows: list[dict] = []
    now = time.time()
    for run_id in run_ids[: max(1, limit)]:
        events = load_run_events(run_id, root=base)
        if not events:
            continue
        first_ts = float(events[0].get("ts", now))
        last_ts = float(events[-1].get("ts", first_ts))
        summary = next((e for e in reversed(events) if e.get("kind") == "run.summary"), None)
        phase_ev = next((e for e in reversed(events) if e.get("kind") == "phase.start"), None)
        phase = (phase_ev or {}).get("payload", {}).get("label")
        attempt = (phase_ev or {}).get("attempt")
        row: dict = {
            "run_id": run_id,
            "events": len(events),
            "elapsed_s": round((last_ts if summary else now) - first_ts, 1),
            "phase": phase,
            "attempt": attempt,
        }
        if summary is not None:
            payload = summary.get("payload") or {}
            row["state"] = "succeeded" if payload.get("succeeded") else "failed"
            row["feature"] = payload.get("feature")
            row["final_run_ref"] = payload.get("final_run_ref")
        elif now - last_ts <= STALE_AFTER_SECONDS:
            row["state"] = "running"
            row["last_event_age_s"] = round(now - last_ts, 1)
        elif _LANE_SUFFIX_RE.search(run_id):
            # A per-lane sub-log (run2 -wN / best-of -cK): it never records its
            # own summary — the parent run's log carries the outcome.
            row["state"] = "lane"
            row["elapsed_s"] = round(last_ts - first_ts, 1)
        else:
            row["state"] = "stale"
            row["elapsed_s"] = round(last_ts - first_ts, 1)
        rows.append(row)
    return rows


def render_status(rows: list[dict]) -> list[str]:
    """Human lines for the status rows."""
    if not rows:
        return ["no recorded runs (runs record events by default; see --verbose)"]
    marks = {"succeeded": "✓", "failed": "✗", "running": "⠿", "stale": "?", "lane": "·"}
    lines = []
    for row in rows:
        mark = marks.get(row["state"], "·")
        head = f"{mark} {row['run_id']}  {row['state']}"
        if row["state"] == "running":
            where = row.get("phase") or "?"
            if row.get("attempt") is not None:
                where += f" (attempt {row['attempt']})"
            head += f" · {where} · {row['elapsed_s']}s elapsed"
        else:
            head += f" · {row['elapsed_s']}s"
        feature = row.get("feature")
        if feature:
            head += f" · {str(feature)[:60]!r}"
        if row.get("final_run_ref"):
            head += f" · ref {row['final_run_ref']}"
        lines.append(head)
    return lines
