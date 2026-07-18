"""Live run progress (#A) + post-hoc worker activity summary (#B).

The supervised loop is otherwise silent between the pack/planning lines and the
final report. ProgressReporter surfaces the PHASES shepherd controls — each
attempt's worker → gate → review — as a live spinner line (TTY) with elapsed
time, committing a ✓/✗ line as each phase settles. On a non-TTY it degrades to
plain committed lines (clean CI logs); a NullProgress silences it entirely (used
by the parallel best-of path, whose interleaving would garble a single spinner).

worker_activity_summary renders a POST-HOC one-liner of what the worker did — its
tool tally read from the durable run trace (best-effort) plus the files it
touched — since the headless worker's internal steps are not available live.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from typing import IO

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _fmt(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


class ProgressReporter:
    """Live phase progress. Use step() to start/advance phases, note() for an
    info line, fail() to mark the current phase failed, close() to finalize."""

    def __init__(self, stream: IO[str] | None = None, enabled: bool | None = None):
        self.stream = stream or sys.stderr
        self.enabled = self.stream.isatty() if enabled is None else enabled
        self._label: str | None = None
        self._phase_start = 0.0
        self._lock = threading.Lock()
        self._spin = itertools.cycle(_SPINNER)
        self._ticker: threading.Thread | None = None
        self._stop = threading.Event()

    # -- output helpers (all failure-tolerant) --------------------------------
    def _write(self, s: str) -> None:
        try:
            self.stream.write(s)
            self.stream.flush()
        except Exception:
            pass

    def _render_live(self) -> None:  # caller holds the lock; TTY only
        if self._label is None:
            return
        frame = next(self._spin)
        self._write(f"\r\033[K{frame} {self._label} · {_fmt(time.monotonic() - self._phase_start)}")

    def _commit(self, marker: str) -> None:  # caller holds the lock
        el = _fmt(time.monotonic() - self._phase_start)
        prefix = "\r\033[K" if self.enabled else ""
        self._write(f"{prefix}{marker} {self._label} ({el})\n")

    def _tick(self) -> None:
        while not self._stop.wait(0.12):
            with self._lock:
                self._render_live()

    def _ensure_ticker(self) -> None:
        if self.enabled and self._ticker is None:
            self._ticker = threading.Thread(target=self._tick, daemon=True)
            self._ticker.start()

    # -- public API -----------------------------------------------------------
    def step(self, label: str) -> None:
        """Finalize the current phase (✓) and begin a new one."""
        with self._lock:
            if self._label is not None:
                self._commit("✓")
            self._label = label
            self._phase_start = time.monotonic()
            if self.enabled:
                self._render_live()
            else:
                self._write(f"▶ {label}\n")
        self._ensure_ticker()

    def fail(self, reason: str | None = None) -> None:
        """Finalize the current phase as failed (✗) with an optional reason."""
        with self._lock:
            if self._label is not None:
                self._commit("✗")
                if reason:
                    self._write(f"   {reason}\n")
                self._label = None

    def note(self, text: str) -> None:
        """Commit an info line above the live spinner line."""
        if not text:
            return
        with self._lock:
            if self.enabled and self._label is not None:
                self._write("\r\033[K")
            self._write(f"   {text}\n")
            if self.enabled and self._label is not None:
                self._render_live()

    def close(self, ok: bool = True) -> None:
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(1)
        with self._lock:
            if self._label is not None:
                self._commit("✓" if ok else "✗")
                self._label = None

    def __enter__(self) -> "ProgressReporter":
        return self

    def __exit__(self, *exc) -> None:
        self.close(ok=exc == (None, None, None))


class NullProgress:
    """A silent reporter — every method is a no-op."""

    def step(self, label: str) -> None: ...
    def fail(self, reason: str | None = None) -> None: ...
    def note(self, text: str) -> None: ...
    def close(self, ok: bool = True) -> None: ...
    def __enter__(self) -> "NullProgress":
        return self
    def __exit__(self, *exc) -> None: ...


def _short_path(path: str, keep: int = 3) -> str:
    """Shorten a jail-absolute tool path to its last ``keep`` parts."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not path.startswith("/") and len(parts) <= keep:
        return path
    tail = parts[-keep:] if len(parts) > keep else parts
    prefix = "…/" if (path.startswith("/") or len(parts) > keep) else ""
    return prefix + "/".join(tail)


def format_event(event: dict, live: bool = True) -> str | None:
    """One rendered line for a run event — the shared vocabulary of the live
    verbose reporter and the post-hoc trace. None = not rendered in this mode
    (live hides phase.* / run.summary: the phase spinner already shows them)."""
    kind = event.get("kind", "")
    p = event.get("payload") or {}
    if kind == "worker.tool":
        target = p.get("target") or ""
        return f"⚒ {p.get('tool', 'tool')} {_short_path(target)}".rstrip()
    if kind == "worker.edit":
        return f"✎ {_short_path(p.get('path', ''))} (+{p.get('add', 0)} −{p.get('del', 0)})"
    if kind == "worker.write":
        extra = f" (+{p['add']} −{p['del']})" if "add" in p else ""
        return f"✚ {_short_path(p.get('path', ''))} ({p.get('lines', 0)} lines){extra}"
    if kind == "worker.tool.fail":
        return f"⚠ tool error: {p.get('error', '')}"
    if kind == "worker.note":
        return f"· {p.get('text', '')}"
    if kind == "worker.raw":
        return f"⚠ oversized stream line dropped ({p.get('bytes', 0)} bytes)"
    if kind == "gate.line":
        return f"┆ {p.get('line', '')}"
    if kind == "gate.test.fail":
        return f"✗ {p.get('test', '?')} ({p.get('framework', '?')})"
    if kind == "gate.result":
        if p.get("infra_error"):
            return f"gate infra error: {p['infra_error']}"
        state = "passed" if p.get("passed") else "failed"
        return f"gate {state} (exit {p.get('exit_code')})"
    if kind == "policy.reject":
        return f"✗ policy: {len(p.get('violations') or [])} violation(s)"
    if kind == "review.verdict":
        return f"review: {'APPROVED' if p.get('approved') else 'REJECTED'}"
    if kind == "review.issue":
        return f"• {p.get('text', '')}"
    if kind == "attempt.diff":
        files = p.get("files") or []
        shown = ", ".join(files[:6]) + ("…" if len(files) > 6 else "")
        return f"changed: {shown}" if files else None
    if live:
        return None  # phase.* / run.summary duplicate the live phase spinner
    if kind == "phase.start":
        return f"▶ {p.get('label', '')}"
    if kind == "phase.fail":
        return f"✗ {p.get('reason', p.get('label', ''))}"
    if kind == "run.summary":
        mark = "✓" if p.get("succeeded") else "✗"
        ref = p.get("final_run_ref")
        return f"{mark} run {'succeeded' if p.get('succeeded') else 'failed'}" + (
            f" · ref {ref}" if ref else ""
        )
    return None


class VerboseReporter(ProgressReporter):
    """A ProgressReporter that also renders run events as sub-lines under the
    live phase — subscribe its handle_event to the RunEventLog."""

    def handle_event(self, event: dict) -> None:
        try:
            line = format_event(event, live=True)
        except Exception:
            return
        if line:
            self.note(line)


def render_trace(events: list[dict], full: bool = False) -> list[str]:
    """Post-hoc timeline of a run's event log. gate.line noise is included
    only with full=True; failures and phases always show."""
    if not events:
        return []
    t0 = events[0].get("ts", 0.0)
    lines: list[str] = []
    for event in events:
        if event.get("kind") == "gate.line" and not full:
            continue
        rendered = format_event(event, live=False)
        if rendered is None:
            continue
        offset = float(event.get("ts", t0)) - float(t0)
        attempt = event.get("attempt")
        tag = f" a{attempt}" if attempt is not None else ""
        lines.append(f"[+{offset:.1f}s{tag}] {rendered}")
    return lines


_TOOL_KINDS = {"tool.call", "tool.call.started"}


def worker_activity_summary(run, entries: dict[str, bytes]) -> str:
    """Post-hoc one-liner of the worker's activity: a tool tally from the run
    trace (best-effort — the trace may be absent) plus the files it touched."""
    names = sorted(entries)
    shown = ", ".join(names[:6]) + ("…" if len(names) > 6 else "")
    files = f"{len(entries)} file(s): {shown}" if names else "no files"
    tools = ""
    try:
        trace = getattr(run, "trace", None)
        if trace is not None:
            tally: dict[str, int] = {}
            for ev in trace.events:
                if ev.get("kind") in _TOOL_KINDS:
                    payload = ev.get("payload") or {}
                    name = payload.get("tool") or payload.get("name") or payload.get("tool_name") or "tool"
                    tally[name] = tally.get(name, 0) + 1
            if tally:
                top = sorted(tally.items(), key=lambda kv: -kv[1])[:6]
                tools = " · tools: " + ", ".join(f"{v}×{k}" for k, v in top)
    except Exception:
        pass
    return f"worker: {files}{tools}"
