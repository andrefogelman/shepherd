"""Line-streamed subprocess execution with a process-group kill on timeout.

The gate's streaming backend (verbose mode). Behaves like
``subprocess.run(capture_output=True)`` — the caller still gets the full merged
output and the exit code — but each output line is also delivered to an
``on_line`` callback as it happens, and a timeout reaps the whole process group
(``start_new_session`` + killpg), consistent with the worker's hard-kill.
stdout and stderr are merged into one chronological stream.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass
class StreamedResult:
    returncode: int | None
    output: str  # merged stdout+stderr, chronological
    timed_out: bool = False


def run_streaming(
    cmd,
    *,
    shell: bool = False,
    cwd=None,
    timeout: float | None = None,
    on_line: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
    output_limit: int | None = None,
) -> StreamedResult:
    """Run ``cmd``, streaming each merged output line to ``on_line`` (stripped
    of its newline; callback errors are swallowed). On timeout the process
    GROUP is SIGKILLed and ``timed_out`` is set. Raises OSError only when the
    command cannot be spawned at all. ``env`` replaces the inherited
    environment wholesale (None = inherit). ``output_limit`` retains only the
    last N characters; callbacks still receive every line. None keeps the
    full output for callers that parse it, rather than just displaying a tail.
    """
    if output_limit is not None and output_limit < 0:
        raise ValueError("output_limit must be non-negative")
    proc = subprocess.Popen(
        cmd,
        shell=shell,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
        env=env,
    )
    chunks: deque[str] = deque()
    retained = 0

    def _retain(text: str) -> None:
        nonlocal retained
        if output_limit == 0:
            return
        if output_limit is not None:
            text = text[-output_limit:]
        chunks.append(text)
        retained += len(text)
        if output_limit is not None:
            while retained > output_limit:
                excess = retained - output_limit
                first = chunks.popleft()
                if len(first) > excess:
                    chunks.appendleft(first[excess:])
                    retained -= excess
                else:
                    retained -= len(first)

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            if on_line is None:
                # No line consumer: even a huge newline-free output can be
                # drained in bounded chunks without readline's large buffer.
                while chunk := proc.stdout.read(64 * 1024):
                    _retain(chunk)
            else:
                for line in proc.stdout:
                    _retain(line)
                    try:
                        on_line(line.rstrip("\n"))
                    except Exception:
                        pass
        except Exception:
            pass

    reader = threading.Thread(target=_reader, daemon=True, name="shepherd-gate-reader")
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(5)
        except Exception:
            pass
    reader.join(2)
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except Exception:
        pass
    return StreamedResult(returncode=proc.returncode, output="".join(chunks), timed_out=timed_out)
