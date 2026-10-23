"""What the jailed worker may reach, decided at launch — pure stdlib.

The jail confines WRITES to the run's clone. It confines nothing else: the
worker reads any path on the machine, inherits this process's whole
environment, and has the network (it needs it for the API). Measured on real
runs: workers with no local toolchain read `~/.ssh/config`, found the ssh
agent socket in their environment, and shipped the repository to a remote
host to compile it there — `ssh <host> "docker run …"` in twenty-odd runs,
one of them `rm -rf` on that host. The reviewer called account-level MCP
connectors. Both agents spawned sub-agents and spent turns on task-list
bookkeeping tools that exist for an interactive session, not a worker.

None of that is the jail's to stop, so this module stops it at the argv:

- tools the worker has no business using are denied (`--disallowedTools`);
- MCP servers are not loaded at all (`--strict-mcp-config` with none named);
- settings come only from the redirected, empty user config
  (`--setting-sources user`), so a repository's own `.claude/settings.json`
  cannot install hooks into the run;
- the browser bridge is off (`--no-chrome`);
- credential-shaped environment variables are unset for the worker's process
  (`env -u NAME`, inserted into the env prefix the substrate already uses).

The jail itself hides one more thing: the sockets of servers that run commands
for the user (the service manager, tmux, screen), from which a command would
start outside the jail. `jail_exposure` tells the user when it cannot.

Everything here is conservative about what it keeps: the variables the API
needs, anything the repository declared through `jail_env`, and anything the
repository lists under `worker.env_keep` are never scrubbed.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

#: Tools a worker or reviewer does not need. Sub-agents multiply cost outside
#: the trace; the task-list and tool-search tools are session furniture; the
#: web tools are network reach the pack was built to make unnecessary; the
#: plan-mode and question tools assume a human on the other end.
DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Agent",
    "Task",
    "TaskCreate",
    "TaskUpdate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "ToolSearch",
    "WebSearch",
    "WebFetch",
    "Skill",
    "EnterPlanMode",
    "ExitPlanMode",
    "AskUserQuestion",
    "SendMessage",
    "Monitor",
)

#: Names unset for the worker outright. Agent sockets and cloud credentials
#: are the ones observed reaching a remote host; the rest are the same shape.
DEFAULT_ENV_SCRUB: tuple[str, ...] = (
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "TWINE_PASSWORD",
    "DOCKER_PASSWORD",
    "KUBECONFIG",
    "VAULT_TOKEN",
    "VERCEL_TOKEN",
    "HF_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    # Addresses of servers that run commands for the user. The jail hides the
    # sockets it can find; these would hand a worker the path of one it cannot
    # (a tmux or dbus socket elsewhere, a remote Docker daemon), and the X
    # display with its cookie lets it type into the user's terminal.
    "TMUX",
    "STY",
    "DBUS_SESSION_BUS_ADDRESS",
    "DOCKER_HOST",
    "DISPLAY",
    "XAUTHORITY",
    # The user's ssh agent: it signs as the user, against this host's own sshd
    # (a login that runs commands outside the jail) and against remote ones.
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
)

#: Any variable whose NAME ends like a secret is unset too. Matched on the
#: name only — values are never read here.
DEFAULT_ENV_SCRUB_PATTERNS: tuple[str, ...] = (
    r"_TOKEN$",
    r"_SECRET$",
    r"_SECRET_KEY$",
    r"_PASSWORD$",
    r"_PASSWD$",
    r"_API_KEY$",
    r"_ACCESS_KEY$",
    r"_PRIVATE_KEY$",
    r"_CREDENTIALS$",
)

#: Never scrubbed, whatever the patterns say: the worker authenticates to the
#: API through these, and scrubbing them is a run that cannot start.
ALWAYS_KEEP_PREFIXES: tuple[str, ...] = ("ANTHROPIC_", "CLAUDE_")

#: Variables that say where the command servers' sockets are. The jail decides
#: what to hide or report from them, in this process, so a repo setting them
#: (`jail_env`) would choose what the jail hides, and keeping them (`env_keep`)
#: would hand the worker a socket the jail does not cover. Neither is ever
#: honoured. TMPDIR is here for macOS, where screen keeps its sockets under it.
JAIL_SOCKET_ENV: frozenset[str] = frozenset({
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "TMUX",
    "TMUX_TMPDIR",
    "STY",
    "SCREENDIR",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DISPLAY",
    "XAUTHORITY",
    "TMPDIR",
})

#: The user's ssh agent. Never kept for the worker, and never set by a repo.
AGENT_ENV: frozenset[str] = frozenset({"SSH_AUTH_SOCK", "SSH_AGENT_PID"})

#: Unset for the worker whatever the policy says: an address the jail does
#: not cover is a way out of it, not a hardening choice. `worker_environment`
#: takes them out of this process's environment for the whole command, so no
#: launch path can hand them on; the host side reads them from `programs`.
_ALWAYS_SCRUBBED: frozenset[str] = (JAIL_SOCKET_ENV | AGENT_ENV) & frozenset(DEFAULT_ENV_SCRUB)

_LANDLOCK_MODULE = "vcs_core._landlock_containment"
_LANDLOCK_NEEDS = ("exposure", "set_outer_prefix", "JailExposureWarning")
_EXPOSURE_NEEDS = ("resolve_programs", "environ")


def _keeps_the_environment() -> bool:
    """True when shepherd-ai (0.3.14 and later) keeps the environment its jail
    reads: what to hide and report, and where bwrap and ssh are."""
    try:
        from vcs_core import _jail_exposure
    except Exception:
        return False
    return all(hasattr(_jail_exposure, name) for name in _EXPOSURE_NEEDS)


def landlock_backend():
    """shepherd-ai's Landlock backend module when it is 0.3.14 or later, else
    None. From 0.3.7 it hides the command servers' sockets, takes an outer
    prefix and warns under its own category; from 0.3.14 it reads what to hide
    from the environment kept before the command's own (`worker_environment`
    removes the servers' addresses from the live one). Every caller here needs
    all of it, so the version is decided in this one place."""
    try:
        from vcs_core import _landlock_containment as landlock
    except Exception:
        return None
    if not all(hasattr(landlock, name) for name in _LANDLOCK_NEEDS):
        return None
    return landlock if _keeps_the_environment() else None


def resolve_jail_programs() -> None:
    """Keep the environment this process has now for the host side, and find
    the programs shepherd-dev and the jail run there on its PATH: bubblewrap
    and ssh (shepherd-ai 0.3.14 and later) and `programs.NAMES`. Called before
    a repo's jail_env or jail_seed enters the environment, so what is set
    there reaches the worker and the gate without choosing those programs or
    what they load."""
    from . import programs

    programs.resolve()
    if _keeps_the_environment():
        from vcs_core import _jail_exposure

        _jail_exposure.resolve_programs()


@contextmanager
def worker_environment():
    """Around a whole command: keep the user's environment for the host side
    (`resolve_jail_programs`), then take the command servers' addresses and the
    ssh agent out of this process's environment until the command ends. The
    worker inherits this environment on every launch path, the fallback ones
    included, so none can hand them on; the jail, the gate and shepherd-dev's
    own subprocesses read them from the kept copy."""
    resolve_jail_programs()
    removed = {name: os.environ.pop(name) for name in sorted(_ALWAYS_SCRUBBED) if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(removed)


def loaded_landlock_backend():
    """`landlock_backend`, but only when a run in this process already
    imported it: clearing state needs nothing loaded that was not."""
    landlock = sys.modules.get(_LANDLOCK_MODULE)
    if landlock is None:
        return None
    return landlock if all(hasattr(landlock, name) for name in _LANDLOCK_NEEDS) else None


def jail_exposure() -> str | None:
    """Why the jail leaves a command server in the worker's reach on this
    host, as a message for the user, or None. On Linux the jail hides the
    service manager's, tmux's and screen's sockets with bubblewrap; without a
    working bubblewrap, or with a writable Docker socket, a worker can have a
    command started outside the jail. On macOS the Seatbelt profile refuses
    connects to tmux's and screen's sockets, and a Docker daemon the user can
    reach is reported. The run goes ahead: the user decides, and a check that
    itself fails is reported the same way rather than stopping the run."""
    if sys.platform == "darwin":
        return _seatbelt_exposure()
    if not sys.platform.startswith("linux"):
        return None
    landlock = landlock_backend()
    if landlock is None:
        return (
            "this shepherd-ai does not hide the user's service manager, tmux or screen from the jail "
            "(needs 0.3.14 or later), so a confined command can start processes outside the jail "
            "through them; run `shepherd-dev update`"
        )
    try:
        jailed, _why = landlock.LandlockContainmentBackend().available()
        if not jailed:
            return None  # no Landlock jail on this host, so nothing for it to hide
        return landlock.exposure()
    except Exception as exc:  # noqa: BLE001 - any failure here is reported, never fatal
        return f"could not check what the jail leaves in reach ({type(exc).__name__}: {exc}); running anyway"


def _seatbelt_exposure() -> str | None:
    try:
        from vcs_core._seatbelt_containment import SeatbeltContainmentBackend
    except Exception:
        return None
    backend = SeatbeltContainmentBackend()
    if not hasattr(backend, "exposure") or not _keeps_the_environment():
        return (
            "this shepherd-ai's macOS sandbox lets a confined command reach the user's tmux and screen "
            "servers (needs 0.3.14 or later), so it can start processes outside the sandbox through them; "
            "run `shepherd-dev update`"
        )
    try:
        if not backend.available()[0]:
            return None
        return backend.exposure()
    except Exception as exc:  # noqa: BLE001 - any failure here is reported, never fatal
        return f"could not check what the sandbox leaves in reach ({type(exc).__name__}: {exc}); running anyway"


#: Flags appended to the CLI argv. `--setting-sources user` loads only the
#: user settings, which live in the redirected (empty) config dir — so a
#: `.claude/settings.json` committed to the repository under review cannot
#: install hooks or permissions into the worker's session.
HARDENING_FLAGS: tuple[str, ...] = (
    "--strict-mcp-config",
    "--no-chrome",
    "--setting-sources",
    "user",
)


@dataclass(frozen=True)
class LaunchPolicy:
    """The decisions, resolved once per command from defaults + repo config."""

    enabled: bool = True
    disallowed_tools: tuple[str, ...] = DEFAULT_DISALLOWED_TOOLS
    env_scrub: tuple[str, ...] = DEFAULT_ENV_SCRUB
    env_scrub_patterns: tuple[str, ...] = DEFAULT_ENV_SCRUB_PATTERNS
    env_keep: tuple[str, ...] = ()
    #: compiled lazily; a dataclass field so equality/hash stay simple
    _patterns: tuple[re.Pattern, ...] = field(default=(), compare=False, repr=False)

    @classmethod
    def from_config(cls, repo_root: Path | None, config: Mapping | None = None) -> "LaunchPolicy":
        """Defaults, adjusted by the repo's `.shepherd-dev.json` `worker` block:

            "worker": {
              "harden": true,
              "disallowed_tools": ["Agent", ...],   # replaces the default list
              "env_scrub": ["EXTRA_VAR"],            # unset in addition
              "env_keep": ["MY_TOOL_TOKEN"]          # never unset
            }

        `jail_env` keys are kept automatically: the repo put them there for
        the worker to see.
        """
        if config is None:
            from .config import jail_env, load_config

            config = load_config(repo_root) if repo_root is not None else {}
            jail_keys = tuple(jail_env(repo_root)) if repo_root is not None else ()
        else:
            raw_jail = config.get("jail_env")
            jail_keys = tuple(k for k in raw_jail) if isinstance(raw_jail, Mapping) else ()
        block = config.get("worker")
        block = block if isinstance(block, Mapping) else {}

        def _names(key: str) -> tuple[str, ...] | None:
            raw = block.get(key)
            if not isinstance(raw, (list, tuple)):
                return None
            return tuple(str(item).strip() for item in raw if str(item).strip())

        enabled = block.get("harden", True)
        disallowed = _names("disallowed_tools")
        extra_scrub = _names("env_scrub") or ()
        keep = _names("env_keep") or ()
        return cls(
            enabled=bool(enabled) if isinstance(enabled, bool) else True,
            disallowed_tools=disallowed if disallowed is not None else DEFAULT_DISALLOWED_TOOLS,
            env_scrub=tuple(dict.fromkeys((*DEFAULT_ENV_SCRUB, *extra_scrub))),
            env_keep=tuple(dict.fromkeys((*keep, *jail_keys))),
        )

    def _compiled(self) -> tuple[re.Pattern, ...]:
        return tuple(re.compile(p) for p in self.env_scrub_patterns)

    def keeps(self, name: str) -> bool:
        if name in JAIL_SOCKET_ENV or name in AGENT_ENV:
            return False
        return name in self.env_keep or name.startswith(ALWAYS_KEEP_PREFIXES)

    def scrub_names(self, environ: Mapping[str, str]) -> list[str]:
        """The variables present in `environ` that the worker must not see.
        With hardening off (`worker.harden: false`, which the repo under
        review can set), the command servers' addresses are still unset."""
        if not self.enabled:
            return sorted(name for name in environ if name in _ALWAYS_SCRUBBED)
        patterns = self._compiled()
        out: list[str] = []
        for name in sorted(environ):
            if self.keeps(name):
                continue
            if name in self.env_scrub or any(p.search(name) for p in patterns):
                out.append(name)
        return out


def _is_env_binary(item: object) -> bool:
    text = str(item)
    return text == "env" or text.endswith("/env")


def _is_claude_body(argv: Iterable[object]) -> bool:
    """Only the `claude -p … --permission-mode …` shape is hardened; a launch
    that is not that CLI keeps its argv untouched."""
    items = [str(a) for a in argv]
    return "-p" in items and "--permission-mode" in items


def harden_argv(
    argv: list, policy: LaunchPolicy | None = None, environ: Mapping[str, str] | None = None
) -> list:
    """The launch argv with the policy applied. A no-op for an argv that is
    not the Claude CLI body; a disabled policy only unsets the command
    servers' addresses."""
    policy = policy or LaunchPolicy()
    if not _is_claude_body(argv):
        return list(argv)
    env = os.environ if environ is None else environ
    out = list(argv)

    # 1. credentials: `env -u NAME …` right after the env binary the substrate
    #    already puts in front of the CLI, so the unset applies to the CLI and
    #    to everything the CLI spawns.
    names = policy.scrub_names(env)
    if names:
        for index, item in enumerate(out):
            if _is_env_binary(item):
                unset: list[str] = []
                for name in names:
                    unset += ["-u", name]
                out[index + 1:index + 1] = unset
                break
    if not policy.enabled:
        return out

    # 2. tools and integrations: appended, once each — an argv that already
    #    carries a flag (a framework that adopted it, a second pass) keeps it.
    present = {str(a) for a in out}
    if policy.disallowed_tools and not present & {"--disallowedTools", "--disallowed-tools"}:
        out += ["--disallowedTools", ",".join(policy.disallowed_tools)]
    if "--strict-mcp-config" not in present:
        out.append("--strict-mcp-config")
    if "--no-chrome" not in present:
        out.append("--no-chrome")
    if "--setting-sources" not in present:
        out += ["--setting-sources", "user"]
    return out


def describe(policy: LaunchPolicy, environ: Mapping[str, str] | None = None) -> dict:
    """What the event log records about a hardened launch: names, never values."""
    env = os.environ if environ is None else environ
    return {
        "enabled": policy.enabled,
        "disallowed_tools": list(policy.disallowed_tools),
        "env_scrubbed": policy.scrub_names(env),
        "flags": [f for f in HARDENING_FLAGS if f.startswith("--")],
    }


# ── Which model serves which role ───────────────────────────────────────────
# Nothing pinned the model before: the provider passed no `--model`, so the
# worker and the reviewer ran on whatever the CLI defaulted to that day, and
# no run recorded which. `--effort` and `--fallback-model` were never passed
# at all. The task id the substrate hands the transport names the role, so
# the choice can be made per role in one place.

#: The CLI's effort levels. An unknown value would fail the launch itself, so
#: config is validated here and an unknown level is dropped with a warning.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "max")

ROLE_BY_TASK: dict[str, str] = {
    "implement": "worker",
    "write_tests": "worker",
    "review": "reviewer",
}


@dataclass(frozen=True)
class RoleModel:
    model: str | None = None
    effort: str | None = None
    fallback: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.model or self.effort or self.fallback)


@dataclass(frozen=True)
class RoleModels:
    """Per-role model choices, resolved once per command.

    Precedence, highest first: explicit CLI flags, the repo's
    `.shepherd-dev.json`, the global `~/.shepherd-dev/config.json`, nothing
    (the CLI's own default, which is what every run used until now).

        "models": {
          "worker":   {"model": "claude-opus-4-8", "effort": "high"},
          "reviewer": {"model": "claude-sonnet-5", "effort": "max",
                       "fallback": "claude-opus-4-8"},
          "planner":  {"model": "claude-haiku-4-5-20251001"}
        }
    """

    worker: RoleModel = RoleModel()
    reviewer: RoleModel = RoleModel()
    planner: RoleModel = RoleModel()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_config(
        cls,
        repo_root: Path | None,
        *,
        config: Mapping | None = None,
        global_config: Mapping | None = None,
        worker_model: str | None = None,
        reviewer_model: str | None = None,
        effort: str | None = None,
    ) -> "RoleModels":
        if config is None or global_config is None:
            from .config import load_config, load_global_config

            if config is None:
                config = load_config(repo_root) if repo_root is not None else {}
            if global_config is None:
                global_config = load_global_config()
        warnings: list[str] = []
        merged: dict[str, dict] = {}
        for source in (global_config, config):  # repo wins: applied last
            block = source.get("models") if isinstance(source, Mapping) else None
            if not isinstance(block, Mapping):
                continue
            for role in ("worker", "reviewer", "planner"):
                raw = block.get(role)
                if isinstance(raw, str):
                    raw = {"model": raw}
                if not isinstance(raw, Mapping):
                    continue
                target = merged.setdefault(role, {})
                for key in ("model", "effort", "fallback"):
                    value = raw.get(key)
                    if isinstance(value, str) and value.strip():
                        target[key] = value.strip()
        if worker_model:
            merged.setdefault("worker", {})["model"] = worker_model.strip()
        if reviewer_model:
            merged.setdefault("reviewer", {})["model"] = reviewer_model.strip()
        if effort:
            for role in ("worker", "reviewer"):
                merged.setdefault(role, {})["effort"] = effort.strip()

        def _role(name: str) -> RoleModel:
            raw = merged.get(name, {})
            level = raw.get("effort")
            if level is not None and level.lower() not in EFFORT_LEVELS:
                warnings.append(
                    f"models.{name}.effort={level!r} is not one of {', '.join(EFFORT_LEVELS)}; ignored"
                )
                level = None
            return RoleModel(
                model=raw.get("model"),
                effort=level.lower() if level else None,
                fallback=raw.get("fallback"),
            )

        return cls(
            worker=_role("worker"),
            reviewer=_role("reviewer"),
            planner=_role("planner"),
            warnings=tuple(warnings),
        )

    def for_task(self, task_id: str) -> RoleModel:
        """The role's choices for a substrate task id; empty for a task that
        is not one of ours (the static smoke task, say)."""
        key = str(task_id or "").rsplit(".", 1)[-1] if str(task_id or "").startswith("shepherd_dev.tasks.") else ""
        role = ROLE_BY_TASK.get(key)
        if role == "worker":
            return self.worker
        if role == "reviewer":
            return self.reviewer
        return RoleModel()

    def describe(self) -> dict:
        out: dict = {}
        for name, role in (("worker", self.worker), ("reviewer", self.reviewer), ("planner", self.planner)):
            if not role.empty:
                out[name] = {k: v for k, v in (("model", role.model), ("effort", role.effort), ("fallback", role.fallback)) if v}
        return out


def model_argv(argv: list, role: RoleModel) -> list:
    """Append the role's `--effort` and `--fallback-model` to a Claude CLI
    argv. `--model` is NOT added here: the provider already emits it from
    its `model` field, which the transport fills from the role (see
    supervisor.set_worker_budget), so it is never added twice."""
    if role.empty or not _is_claude_body(argv):
        return list(argv)
    out = list(argv)
    present = {str(a) for a in out}
    if role.effort and "--effort" not in present:
        out += ["--effort", role.effort]
    if role.fallback and "--fallback-model" not in present:
        out += ["--fallback-model", role.fallback]
    return out
