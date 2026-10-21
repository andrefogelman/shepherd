"""Opt-in memory ceiling for what a run launches on Linux.

The jail confines where a worker may write, not how much memory the commands
it runs may take. A test a worker wrote grew to 19.8 GB on a shared Linux host
and the kernel's global OOM killer had to pick a victim from every process on
the machine. With `limits.memory_max` (or `--memory-max`) set, each worker and
reviewer launch and each local gate run gets its own cgroup scope through
`systemd-run --user --scope`, with `MemoryMax` at the limit and no swap. An
overrun is then killed inside that scope and nowhere else.

A limit that was asked for and cannot be enforced (not Linux, no
`systemd-run`, no user manager, memory controller not delegated) stops the
run before it starts: running without the ceiling the user asked for is the
outcome the option exists to prevent. With no limit set nothing here runs and
every argv is left exactly as it was.

The active limit is process state, set once per run by the CLI like
`set_worker_budget`, because the launches it governs are built deep inside
the substrate's transport and the gate, far from any argument list.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys

#: Below this a Claude worker cannot start at all; a smaller limit is a typo.
MIN_BYTES = 256 * 1024 * 1024

_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
_SIZE = re.compile(r"^\s*(\d+)\s*([KMGT]?)i?B?\s*$", re.IGNORECASE)

_active: int | None = None


def parse_size(value: object) -> int:
    """Bytes from `8G`, `512M`, `8GiB` or a plain number of bytes. Raises
    ValueError with the reason; the ceiling must be at least MIN_BYTES."""
    if isinstance(value, bool):
        raise ValueError(f"not a memory size: {value!r}")
    if isinstance(value, int):
        number = value
    else:
        match = _SIZE.match(str(value))
        if not match:
            raise ValueError(f"not a memory size: {value!r} (use e.g. 8G or 512M)")
        number = int(match.group(1)) * _UNITS[match.group(2).upper()]
    if number < MIN_BYTES:
        raise ValueError(f"memory limit {value!r} is below the 256M minimum a worker needs")
    return number


def runtime_dir() -> str | None:
    """The user manager's runtime directory, where its bus lives. `sudo -i`
    and some service managers leave XDG_RUNTIME_DIR unset, so fall back to
    the standard path for this uid."""
    explicit = os.environ.get("XDG_RUNTIME_DIR")
    if explicit and os.path.isdir(explicit):
        return explicit
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    candidate = f"/run/user/{getuid()}"
    return candidate if os.path.isdir(candidate) else None


def scope_argv(limit: int, rundir: str) -> list[str]:
    """The prefix that runs a command in its own memory-capped scope. The
    command is exec'd in place, so its pid, process group and session are the
    caller's: the launch perl's group kill and the watchdog see no change.
    `--collect` drops the unit even when the scope ended in an OOM kill."""
    return [
        "env", f"XDG_RUNTIME_DIR={rundir}",
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        "-p", f"MemoryMax={limit}", "-p", "MemorySwapMax=0",
        "--",
    ]


#: Prints the memory.max of the cgroup the shell landed in.
_READ_OWN_MAX = 'cat "/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)/memory.max"'


def unavailable_reason(limit: int, *, run=subprocess.run) -> str | None:
    """None when `limit` can be enforced here, else why not. The check runs
    a real scope and reads back the memory.max it got: a scope that starts
    without the controller delegated would carry no limit at all."""
    if sys.platform != "linux":
        return f"a memory limit needs Linux cgroups (systemd-run --user); this platform is {sys.platform}"
    if shutil.which("systemd-run") is None:
        return "a memory limit needs systemd-run, which is not on PATH"
    rundir = runtime_dir()
    if rundir is None:
        return "no user runtime directory (XDG_RUNTIME_DIR or /run/user/<uid>): no systemd user manager to ask"
    try:
        proc = run(
            [*scope_argv(limit, rundir), "sh", "-c", _READ_OWN_MAX],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"systemd-run could not start a scope: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
        return f"systemd-run could not start a scope: {detail[0]}"
    got = (proc.stdout or "").strip()
    if got != str(limit):
        return (
            f"the scope came up with memory.max {got or 'unreadable'}, not {limit}: "
            "the memory controller is not delegated to this user"
        )
    return None


def activate(limit: int | None, *, run=subprocess.run) -> str | None:
    """Make `limit` the ceiling for this process's launches, or clear it with
    None. Returns why it cannot be enforced, leaving nothing active, or None."""
    global _active
    _active = None
    if limit is None:
        return None
    reason = unavailable_reason(limit, run=run)
    if reason is None:
        _active = limit
    return reason


def active() -> int | None:
    return _active


def _restore_env(rundir: str) -> list[str]:
    """What runs between the scope and the command so the command sees the
    XDG_RUNTIME_DIR it would have seen without the limit. systemd-run needs
    the variable to find the user bus; a jailed worker handed it gets the
    bus's path for free, and from that bus a transient service runs outside
    the jail."""
    original = os.environ.get("XDG_RUNTIME_DIR")
    if original is None:
        return ["env", "-u", "XDG_RUNTIME_DIR"]
    if original != rundir:
        return ["env", f"XDG_RUNTIME_DIR={original}"]
    return []


def wrap_argv(argv: list) -> list:
    """`argv` inside the active scope; unchanged when no limit is active."""
    if _active is None:
        return argv
    rundir = runtime_dir()
    if rundir is None:  # checked at activation; the directory vanished since
        raise RuntimeError("memory limit active but the user runtime directory is gone")
    return [*scope_argv(_active, rundir), *_restore_env(rundir), *argv]


def wrap_shell(command: str) -> str:
    """A `shell=True` command string inside the active scope; unchanged when
    no limit is active."""
    if _active is None:
        return command
    return " ".join(shlex.quote(part) for part in wrap_argv(["sh", "-c", command]))


def describe(limit: int) -> str:
    for unit in ("T", "G", "M"):
        size = _UNITS[unit]
        if limit % size == 0:
            return f"{limit // size}{unit}"
    return f"{limit} bytes"
