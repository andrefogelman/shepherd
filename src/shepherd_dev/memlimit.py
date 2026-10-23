"""Opt-in memory ceiling for what a run launches on Linux.

The jail confines where a worker may write, not how much memory the commands
it runs may take. A test a worker wrote grew to 19.8 GB on a shared Linux host
and the kernel's global OOM killer had to pick a victim from every process on
the machine. With `limits.memory_max` (or `--memory-max`) set, each worker and
reviewer launch and each local gate run gets its own cgroup scope through
`systemd-run --user --scope`, with `MemoryMax` at the limit and no swap. An
overrun is then killed inside that scope and nowhere else.

A limit that was asked for and cannot be enforced (not Linux, no
`systemd-run`, no user manager, memory controller not delegated, no Landlock
jail on the host, a shepherd-ai older than 0.3.14) stops the run before it
starts: running without the ceiling the user asked for is the
outcome the option exists to prevent. With no limit set nothing here runs and
every argv is left exactly as it was.

The active limit is process state, set once per run by the CLI like
`set_worker_budget`, because the launches it governs are built deep inside
the substrate's transport and the gate, far from any argument list.

Worker and reviewer launches get the scope from the jail itself
(`set_outer_prefix` in shepherd-ai's Landlock backend), so it wraps the jail
from outside. Inside the jail it could not start: the jail hides the user
manager's bus, because from that bus a command runs outside the jail.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys

from . import programs

#: Below this a Claude worker cannot start at all; a smaller limit is a typo.
MIN_BYTES = 256 * 1024 * 1024

_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
_SIZE = re.compile(r"^\s*(\d+)\s*([KMGT]?)i?B?\s*$", re.IGNORECASE)

#: systemd-run expands ${...} and $$ in the command it runs since this version.
_EXPANDS_SINCE = 254

_active: int | None = None
_no_expand = True


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


def scope_argv(limit: int, rundir: str, *, no_expand: bool = False) -> list[str]:
    """The prefix that runs a command in its own memory-capped scope. The
    command is exec'd in place, so its pid, process group and session are the
    caller's: the launch perl's group kill and the watchdog see no change.
    Around the jail, what it execs may be bubblewrap, which forks the jailed
    runner as its child; that child dies when bubblewrap does
    (`--die-with-parent`).
    `--collect` drops the unit even when the scope ended in an OOM kill.
    `no_expand` keeps systemd-run from rewriting `${...}` and `$$` in the
    command, a worker prompt or a gate string; versions before 254 do not
    expand and reject the flag."""
    return [
        programs.argv0("env"), f"XDG_RUNTIME_DIR={rundir}",
        programs.argv0("systemd-run"), "--user", "--scope", "--quiet", "--collect",
        *(["--expand-environment=no"] if no_expand else []),
        "-p", f"MemoryMax={limit}", "-p", "MemorySwapMax=0",
        "--",
    ]


def _systemd_version(run) -> int | None:
    try:
        proc = run(
            [programs.argv0("systemd-run"), "--version"],
            capture_output=True, text=True, timeout=30, env=programs.host_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"\s*systemd\s+(\d+)", proc.stdout or "")
    return int(match.group(1)) if match else None


#: Prints the memory.max of the cgroup the shell landed in.
_READ_OWN_MAX = 'cat "/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)/memory.max"'


def unavailable_reason(limit: int, *, run=subprocess.run) -> str | None:
    """None when `limit` can be enforced here, else why not. The check runs
    a real scope and reads back the memory.max it got: a scope that starts
    without the controller delegated would carry no limit at all."""
    if sys.platform != "linux":
        return f"a memory limit needs Linux cgroups (systemd-run --user); this platform is {sys.platform}"
    if programs.which("systemd-run") is None:
        return "a memory limit needs systemd-run, which is not on PATH"
    rundir = runtime_dir()
    if rundir is None:
        return "no user runtime directory (XDG_RUNTIME_DIR or /run/user/<uid>): no systemd user manager to ask"
    version = _systemd_version(run)
    if version is None:
        return "could not read systemd-run's version, so whether it rewrites ${...} in the command is unknown"
    try:
        proc = run(
            [*scope_argv(limit, rundir, no_expand=version >= _EXPANDS_SINCE), programs.argv0("sh"), "-c", _READ_OWN_MAX],
            capture_output=True, text=True, timeout=30, env=programs.host_env(),
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
    global _active, _no_expand
    _active = None
    _clear_jail_prefix()
    if limit is None:
        return None
    reason = unavailable_reason(limit, run=run)
    if reason is not None:
        return reason
    from .launch import landlock_backend

    landlock = landlock_backend()
    if landlock is None:
        return "this shepherd-ai cannot put the scope around its jail (needs 0.3.14 or later)"
    try:
        jailed, why = landlock.LandlockContainmentBackend().available()
    except Exception as exc:  # noqa: BLE001 - a failed check refuses the limit, like any other reason
        return f"could not check the Landlock jail ({type(exc).__name__}: {exc})"
    if not jailed:
        # worker and reviewer launches would not go through the jail, so the
        # scope around it would never run and the limit would hold the gate only
        return f"the scope wraps the Landlock jail, and this host runs without one ({why})"
    rundir = runtime_dir()
    if rundir is None:  # unavailable_reason found it a moment ago
        return "the user runtime directory went away"
    version = _systemd_version(run)
    # unreadable here means the flag stays: a launch that fails on it is
    # loud, a command rewritten under it is not
    no_expand = version is None or version >= _EXPANDS_SINCE
    # built once: every worker and reviewer launch gets this exact prefix
    prefix = [*scope_argv(limit, rundir, no_expand=no_expand), *_restore_env(rundir)]
    try:
        landlock.set_outer_prefix(prefix)
    except Exception as exc:
        _clear_jail_prefix()
        return f"shepherd-ai did not take the scope around its jail: {exc}"
    _active, _no_expand = limit, no_expand
    return None


def _clear_jail_prefix() -> None:
    # only a module some run already loaded can hold a prefix, so a run that
    # never loaded it has nothing to clear
    from .launch import loaded_landlock_backend

    landlock = loaded_landlock_backend()
    if landlock is not None:
        landlock.set_outer_prefix([])


def active() -> int | None:
    return _active


def _restore_env(rundir: str) -> list[str]:
    """What runs between the scope and the command so the command sees the
    XDG_RUNTIME_DIR it would have seen without the limit. systemd-run needs
    the variable to find the user bus; nothing after the scope should inherit
    a setting the user did not have."""
    original = os.environ.get("XDG_RUNTIME_DIR")
    if original is None:
        return [programs.argv0("env"), "-u", "XDG_RUNTIME_DIR"]
    if original != rundir:
        return [programs.argv0("env"), f"XDG_RUNTIME_DIR={original}"]
    return []


def wrap_argv(argv: list) -> list:
    """`argv` inside the active scope; unchanged when no limit is active."""
    if _active is None:
        return argv
    # Worker and reviewer launches do not come through here: their prefix was
    # built once by `activate`. This path serves the gate and pre_gate_cmd,
    # which build theirs per call, so the directory is checked per call too.
    rundir = runtime_dir()
    if rundir is None:  # checked at activation; the directory vanished since
        raise RuntimeError("memory limit active but the user runtime directory is gone")
    return [*scope_argv(_active, rundir, no_expand=_no_expand), *_restore_env(rundir), *argv]


def wrap_shell(command: str) -> str:
    """A `shell=True` command string inside the active scope; unchanged when
    no limit is active."""
    if _active is None:
        return command
    return " ".join(shlex.quote(part) for part in wrap_argv([programs.argv0("sh"), "-c", command]))


def describe(limit: int) -> str:
    for unit in ("T", "G", "M"):
        size = _UNITS[unit]
        if limit % size == 0:
            return f"{limit // size}{unit}"
    return f"{limit} bytes"
