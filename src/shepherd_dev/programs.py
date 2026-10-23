"""The environment and programs of the host side, kept before a repo's settings apply.

A repo's `jail_env` and `jail_seed` sit in this process's environment for the
whole command, because that is how they reach the worker and the gate. What
this process runs for itself on the host (git, the planner's and the auth
probe's claude, ssh to a remote gate, cp, notify-send, the `systemd-run` that
holds the memory ceiling) must not see them: a `PATH` there would choose the
binary, and `NODE_OPTIONS`, `GIT_CONFIG_*` or `HOME` would choose what it
loads. `resolve` keeps a copy of the environment the user started with, once
per process; `host_env` and `which` answer from it. Until `resolve` is called,
each use reads the live environment as it always did.
"""

from __future__ import annotations

import errno
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

NAMES = ("systemd-run", "env", "sh", "git")

_found: dict[str, str | None] = {}
_environ: dict[str, str] | None = None


def resolve() -> None:
    """Keep the current environment for the host side, and find every program
    in `NAMES` on its PATH now, a program not found included."""
    global _environ
    if _environ is None:
        _environ = dict(os.environ)
    for name in NAMES:
        if name not in _found:
            _found[name] = shutil.which(name, path=_search_path(_environ))


def environ() -> Mapping[str, str]:
    """The environment `resolve` kept, else the live one."""
    return _environ if _environ is not None else os.environ


def host_env() -> dict[str, str]:
    """A copy of `environ`, for a subprocess run on the host for shepherd-dev itself."""
    return dict(environ())


def home() -> Path:
    """The user's home in `environ`, as `Path.home` reads it from the live one."""
    value = environ().get("HOME")
    return Path(value) if value else Path.home()


def which(name: str) -> str | None:
    """Where `name` is on the PATH of `environ`."""
    if name in _found:
        return _found[name]
    if _environ is None:
        return shutil.which(name, path=os.environ.get("PATH"))
    return shutil.which(name, path=_search_path(_environ))


def _search_path(environment: Mapping[str, str]) -> str:
    """The PATH of `environment` with only its absolute entries, `os.defpath`
    when it has none. shutil.which with no path reads the PATH of the moment,
    which may be the repo's; it reads an empty or relative entry as the
    current directory, the repo's when shepherd-dev runs there; and for an
    empty one it returns the bare name, which exec looks up again."""
    entries = environment.get("PATH", os.defpath).split(os.pathsep)
    return os.pathsep.join(entry for entry in entries if os.path.isabs(entry))


def argv0(name: str) -> str:
    """What to put first in an argv for `name`. Before `resolve` it is the bare
    name, looked up at exec time as before. After it, the path found on the
    user's PATH; a program missing there raises FileNotFoundError, as exec
    would, rather than fall back to the PATH of the moment, which may be the
    repo's."""
    if _environ is None:
        return _found.get(name) or name
    path = which(name)
    if path is None:
        raise FileNotFoundError(errno.ENOENT, f"{name} is not on the PATH shepherd-dev started with", name)
    return path
