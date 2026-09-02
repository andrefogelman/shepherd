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
