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
from heapq import nlargest
from pathlib import Path
from typing import Iterable

from .events import iter_run_events

#: A run with no summary and no event for this long is presumed dead.
STALE_AFTER_SECONDS = 30 * 60


def _as_ts(value, fallback: float) -> float:
    """A timestamp off disk, or `fallback` when it is not one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback

_LANE_SUFFIX_RE = re.compile(r"-(w\d+|c\d+|wa|wb)$")


def runs_status(root: Path | None = None, limit: int = 10) -> list[dict]:
    """Status rows for the most recent runs, newest first."""
    from .events import _default_runs_root

    base = Path(root) if root else _default_runs_root()
    try:
        run_ids = nlargest(max(1, limit), (p.name for p in base.iterdir() if p.is_dir()))
    except Exception:
        return []
    rows: list[dict] = []
    now = time.time()
    for run_id in run_ids:
        count = 0
        first_ts = last_ts = now
        summary: dict | None = None
        phase_ev: dict | None = None

        def tracked_events():
            nonlocal count, first_ts, last_ts, summary, phase_ev
            for event in iter_run_events(run_id, root=base):
                if count == 0:
                    first_ts = _as_ts(event.get("ts"), now)
                count += 1
                last_ts = _as_ts(event.get("ts"), first_ts)
                if event.get("kind") == "run.summary":
                    summary = event
                elif event.get("kind") == "phase.start":
                    phase_ev = event
                yield event

        # Every call rereads disk, but only aggregates survive each event.
        # Verbose gate output can dominate a log; status needs none of its text.
        metrics = run_telemetry(tracked_events())
        if not count:
            continue
        # A run's own log can be truncated or hand-edited, and float() on a
        # bad `ts` raised out of this loop — so ONE damaged run made `status`
        # list none of them. Status is the tool you reach for when something
        # already went wrong; it has to survive the wreckage it reports on.
        phase = (phase_ev or {}).get("payload", {}).get("label")
        attempt = (phase_ev or {}).get("attempt")
        row: dict = {
            "run_id": run_id,
            "events": count,
            "elapsed_s": round((last_ts if summary else now) - first_ts, 1),
            "phase": phase,
            "attempt": attempt,
            **metrics,
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


def run_telemetry(events: Iterable[dict]) -> dict:
    """Tokens, cost, launches and models summed over a run's `worker.result`
    events (worker attempts and reviewer alike). Empty when none were
    recorded — a run whose stream was not tailed, or one killed mid-launch."""
    tokens_in = tokens_out = cached = 0
    cost = 0.0
    launches = 0
    models: set[str] = set()

    def counted_events():
        nonlocal tokens_in, tokens_out, cached, cost, launches
        for event in events:
            if event.get("kind") == "worker.result":
                p = event.get("payload") or {}
                launches += 1
                tokens_in += int(p.get("input_tokens") or 0)
                tokens_out += int(p.get("output_tokens") or 0)
                cached += int(p.get("cache_read_input_tokens") or 0)
                try:
                    cost += float(p.get("total_cost_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
                if p.get("model"):
                    models.add(str(p["model"]))
                for name in p.get("models") or []:
                    models.add(str(name))
            yield event

    behaviour = behaviour_metrics(counted_events())
    out: dict = {}
    if launches:
        out.update({
            "launches": launches,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_cached": cached,
            "cost_usd": round(cost, 4),
            "models": sorted(models),
        })
    out.update(behaviour)
    return out


_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def behaviour_metrics(events: Iterable[dict]) -> dict:
    """How the agents spent their turns, from the tool events:

    - `explore_calls`: tool calls in the FIRST worker attempt before its
      first edit — the "where do I even start" cost the context pack exists
      to remove (median 18 before the prompt was rendered as a document);
    - `review_tools`: tool calls made by the reviewer across the run — the
      cost of a reviewer navigating a tree instead of reading a diff.

    Empty when the run recorded no worker tools (verbose off)."""
    phase = None
    first_worker_seen = False
    counting_explore = False
    explore = 0
    review_tools = 0
    any_tools = False
    for event in events:
        kind = event.get("kind")
        payload = event.get("payload") or {}
        if kind == "phase.start":
            phase = payload.get("label")
            if phase == "worker" and not first_worker_seen:
                first_worker_seen = True
                counting_explore = True
            elif phase != "worker":
                counting_explore = False
            continue
        if kind != "worker.tool":
            continue
        any_tools = True
        tool = str(payload.get("tool") or "")
        if phase == "review":
            review_tools += 1
        elif counting_explore:
            if tool in _EDIT_TOOLS:
                counting_explore = False
            else:
                explore += 1
    if not any_tools:
        return {}
    return {"explore_calls": explore, "review_tools": review_tools}


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
        if row.get("launches"):
            head += (
                f" · {row['tokens_in']:,} in / {row['tokens_out']:,} out"
                f" · ${row['cost_usd']:.2f} · {row['launches']} launch(es)"
            )
            if row.get("models"):
                head += f" · {', '.join(row['models'])}"
        if "explore_calls" in row:
            head += f" · explore {row['explore_calls']} · review tools {row['review_tools']}"
        lines.append(head)
    return lines
