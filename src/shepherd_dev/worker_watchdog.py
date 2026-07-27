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


def _still_the_same(pids: list[int], snapshot: dict[int, tuple[int, str]]) -> list[int]:
    """Of `pids`, those whose (ppid, command) still match what the snapshot saw.

    A pid is not an identity. The targets are chosen from a `ps` snapshot and
    signalled afterwards — with a whole grace period between SIGTERM and
    SIGKILL — and a pid freed in that window is handed straight to the next
    process the OS starts. Signalling it would kill a stranger.

    Re-reading `ps` and demanding the same parent and command line narrows the
    window from "the whole kill sequence" to "one ps call", and costs one
    process listing. It is not proof — a pid could in principle be reused by
    something with an identical command under the same parent — but that is a
    different order of unlikelihood from mere reuse. `pidfd_open` would be
    proof, and is Linux-only; the watchdog runs on darwin too.
    """
    current = _read_proctable()
    if not current:  # ps unavailable: no evidence either way, so change nothing
        return list(pids)
    return [p for p in pids if p in current and current[p] == snapshot.get(p)]


def _kill_subtree(
    pids: set[int],
    grace: float = 3.0,
    snapshot: dict[int, tuple[int, str]] | None = None,
) -> int:
    """SIGTERM then (after grace) SIGKILL the given pids. Returns count signaled.

    With `snapshot` (the process table the pids were chosen from), each pid's
    identity is re-checked immediately before each signal, so a pid recycled
    between selection and the kill is spared. Without one the behaviour is
    exactly as it was.
    """
    live = [p for p in pids if p != os.getpid()]
    if snapshot is not None:
        live = _still_the_same(live, snapshot)
    if not live:
        return 0

    for p in live:
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass
    signalled = len(live)

    time.sleep(grace)
    # Re-check again: the grace is the widest part of the window, and a target
    # that died from the SIGTERM has been freeing its pid throughout it.
    if snapshot is not None:
        live = _still_the_same(live, snapshot)
    for p in live:
        try:
            os.kill(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass
    return signalled


#: Seconds past the budget before the backstop escalates.
#:
#: The launch perl's SIGALRM fires exactly AT the budget and kills the worker's
#: process group; this watchdog only exists for the case where that kill did not
#: happen (setsid blocked, killpg denied, the perl seam moved under a framework
#: upgrade). The grace covers the orderly teardown after a successful kill —
#: milliseconds of stream drain and transport shutdown, not tens of seconds.
#:
#: So a shorter grace cannot punish a worker the alarm already reaped: by then
#: find_worker_pids finds nothing and the watchdog is a no-op. It only shortens
#: the wait in exactly the case the backstop is FOR — up to 45s per overrun
#: attempt, and an attempt that overruns is one the run is already paying for.
DEFAULT_GRACE = 15


class WorkerWatchdog:
    """Backstop that hard-kills the worker subtree `grace` seconds past the budget.

    Use as a context manager around the (blocking) worker call; __exit__ cancels
    it, so a worker that finishes in time is never touched. fired tells whether a
    kill happened, so the caller can record the attempt as timed-out."""

    def __init__(
        self, budget_seconds: int, grace: int = DEFAULT_GRACE, own_pid: int | None = None
    ):
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
            # Hand the table along: it is what these pids were selected from,
            # so the kill can confirm each one is still that process.
            _kill_subtree(pids, snapshot=table)

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
