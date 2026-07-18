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
) -> StreamedResult:
    """Run ``cmd``, streaming each merged output line to ``on_line`` (stripped
    of its newline; callback errors are swallowed). On timeout the process
    GROUP is SIGKILLed and ``timed_out`` is set. Raises OSError only when the
    command cannot be spawned at all."""
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
    )
    lines: list[str] = []

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)
                if on_line is not None:
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
    return StreamedResult(returncode=proc.returncode, output="".join(lines), timed_out=timed_out)
