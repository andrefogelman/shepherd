"""Worker budget hard-kill backstop (#B).

The framework's per-worker budget is a perl `alarm; exec` that SIGALRMs the
runner but does not reap the descendant tree (claude + MCP subprocesses orphan),
and shepherd's budget rebind is a fragile private-API path. This watchdog
enforces the budget at shepherd's OWN process level: a daemon thread that, `grace`
seconds after the budget, discovers the worker's process subtree — the runner
process among THIS process's descendants (its argv carries runner.mjs), plus
everything under it — and SIGTERM→SIGKILLs it. So a stuck worker dies here, not
at the outer safety timeout, and leaves no orphan.

Scoped to the serial run: best-of runs workers in parallel and a signature-based
kill can't tell them apart, so those keep the framework's own alarm. The kill is
confined to descendants of this process whose argv names the runner, so a user's
own interactive `claude` session (not our descendant, no runner.mjs) is never hit.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

# argv fragments that identify a shepherd worker LAUNCH ROOT among our
# descendants. The Claude worker is `perl -e 'alarm …; exec @ARGV' <budget>
# <claude> -p <prompt> …` — the perl script sits at argv[2], BEFORE the (huge)
# prompt, so it survives `ps` truncation; `exec @ARGV` is in both the base and
# the #A killtree perl. runner.mjs/codex_runner.mjs cover the Codex provider.
# Seeding on the root and taking its whole subtree reaps claude + MCP children.
_WORKER_MARKERS = ("exec @ARGV", "runner.mjs", "codex_runner.mjs")


def _read_proctable() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, command) for every process. macOS + Linux via ps."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return {}
    table: dict[int, tuple[int, str]] = {}
    for line in out.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2] if len(parts) > 2 else "")
    return table


def _descendants(root: int, table: dict[int, tuple[int, str]]) -> set[int]:
    """All descendant pids of root (root itself excluded)."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _cmd) in table.items():
        children.setdefault(ppid, []).append(pid)
    out: set[int] = set()
    stack = list(children.get(root, []))
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.add(pid)
        stack.extend(children.get(pid, []))
    return out


def find_worker_pids(own_pid: int, table: dict[int, tuple[int, str]]) -> set[int]:
    """Worker pids to kill: the runner processes among own_pid's descendants
    (argv names the runner script) plus their whole subtrees. Nothing outside
    this process's descendant tree is ever selected."""
    desc = _descendants(own_pid, table)
    targets: set[int] = set()
    for pid in desc:
        cmd = table.get(pid, (0, ""))[1]
        if any(m in cmd for m in _WORKER_MARKERS):
            targets.add(pid)
            targets |= _descendants(pid, table)
    targets.discard(own_pid)
    return targets


def _kill_subtree(pids: set[int], grace: float = 3.0) -> int:
    """SIGTERM then (after grace) SIGKILL the given pids. Returns count signaled."""
    live = [p for p in pids if p != os.getpid()]
    for p in live:
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass
    if live:
        time.sleep(grace)
    for p in live:
        try:
            os.kill(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass
    return len(live)


class WorkerWatchdog:
    """Backstop that hard-kills the worker subtree `grace` seconds past the budget.

    Use as a context manager around the (blocking) worker call; __exit__ cancels
    it, so a worker that finishes in time is never touched. fired tells whether a
    kill happened, so the caller can record the attempt as timed-out."""

    def __init__(self, budget_seconds: int, grace: int = 60, own_pid: int | None = None):
        self.after = max(1, budget_seconds) + max(0, grace)
        self.grace = grace
        self._own_pid = own_pid or os.getpid()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fired = False

    @property
    def fired(self) -> bool:
        return self._fired

    def _watch(self) -> None:
        if self._stop.wait(self.after):
            return  # cancelled: the worker finished within budget + grace
        table = _read_proctable()
        pids = find_worker_pids(self._own_pid, table)
        if pids:
            self._fired = True
            _kill_subtree(pids)

    def start(self) -> "WorkerWatchdog":
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.grace + 5)

    def __enter__(self) -> "WorkerWatchdog":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.cancel()
