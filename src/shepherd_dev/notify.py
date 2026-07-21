"""Desktop notification when shepherd needs the human — best-effort, silent.

Runs take minutes and finish in a terminal the user may have left; the
settlement decision then sits waiting unseen. One native notification when a
decision is ready fixes that (macOS `osascript`, Linux `notify-send`; other
platforms are a no-op). Opt out with ``SHEPHERD_DEV_NO_NOTIFY=1``. Any failure
is swallowed — a notification must never affect a run.
"""

from __future__ import annotations

import os
import subprocess
import sys


def notify(title: str, message: str) -> None:
    if os.environ.get("SHEPHERD_DEV_NO_NOTIFY"):
        return
    try:
        if sys.platform == "darwin":
            # argv form (no shell); escape for the AppleScript string literal
            def esc(s: str) -> str:
                return s.replace("\\", "\\\\").replace('"', '\\"')

            script = f'display notification "{esc(message)}" with title "{esc(title)}"'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", title, message], capture_output=True, timeout=5)
    except Exception:
        pass
