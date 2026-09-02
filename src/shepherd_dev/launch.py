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

Everything here is conservative about what it keeps: the variables the API
needs, anything the repository declared through `jail_env`, and anything the
repository lists under `worker.env_keep` are never scrubbed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
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
        return name in self.env_keep or name.startswith(ALWAYS_KEEP_PREFIXES)

    def scrub_names(self, environ: Mapping[str, str]) -> list[str]:
        """The variables present in `environ` that the worker must not see."""
        if not self.enabled:
            return []
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
    """The launch argv with the policy applied. A no-op for a disabled policy
    or an argv that is not the Claude CLI body."""
    policy = policy or LaunchPolicy()
    if not policy.enabled or not _is_claude_body(argv):
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
