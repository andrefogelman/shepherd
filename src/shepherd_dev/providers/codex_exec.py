"""Codex worker executors — run outside shepherd-ai, no Claude dependency.

Executors edit an isolated clone directory in place via `codex exec`, the
official headless mode of the OpenAI Codex CLI. Isolation is double: the L1
host layer clones the repo, and `codex exec --sandbox workspace-write` adds the
Codex CLI's own OS sandbox (Seatbelt on macOS, Landlock on Linux) around every
model-generated shell command. The host layer then diffs the clone against the
original repo to build a proposal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .hosted import ExecResult, HostedExecutor

DEFAULT_SANDBOX = "workspace-write"


def find_codex_bin(explicit: str | None = None) -> str | None:
    """Resolve the Codex CLI binary. Env SHEPHERD_DEV_CODEX_CMD wins, then PATH."""
    if explicit:
        return explicit
    env = os.environ.get("SHEPHERD_DEV_CODEX_CMD")
    if env:
        return env
    for name in ("codex",):
        found = shutil.which(name)
        if found:
            return found
    # Common install locations (npm global, hermes-bundled node bin)
    for candidate in (
        Path.home() / ".hermes" / "node" / "bin" / "codex",
        Path("/usr/local/bin/codex"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


class FakeCodexExecutor:
    """Test/offline executor: applies a fixed set of file writes to the clone."""

    def __init__(self, files: dict[str, bytes] | None = None, fail: bool = False, error: str = "fake fail"):
        self.files = files or {}
        self.fail = fail
        self.error = error

    def run(self, clone: Path, prompt: str, *, budget_seconds: int) -> ExecResult:
        started = time.monotonic()
        if self.fail:
            return ExecResult(False, self.error, round(time.monotonic() - started, 1))
        for rel, content in self.files.items():
            target = (clone / rel).resolve()
            if not target.is_relative_to(clone.resolve()):
                return ExecResult(False, f"fake path escapes clone: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return ExecResult(True, None, round(time.monotonic() - started, 1), "fake executor wrote files")


class CliCodexExecutor:
    """Invoke `codex exec` as a headless coding agent on `clone`."""

    def __init__(
        self,
        *,
        codex_bin: str | None = None,
        model: str | None = None,
        sandbox: str | None = None,
        extra_args: list[str] | None = None,
    ):
        self.codex_bin = codex_bin or find_codex_bin()
        self.model = model or os.environ.get("SHEPHERD_DEV_CODEX_MODEL")
        self.sandbox = sandbox or os.environ.get("SHEPHERD_DEV_CODEX_SANDBOX") or DEFAULT_SANDBOX
        self.extra_args = list(extra_args or [])

    def build_argv(self, clone: Path, prompt: str) -> list[str]:
        # `codex exec` is non-interactive by design: no approval prompts; writes
        # are confined to the -C workdir by the sandbox policy. The clone has no
        # .git (copytree excludes it), hence --skip-git-repo-check; --ephemeral
        # keeps session files off disk.
        argv = [
            self.codex_bin or "codex",
            "exec",
            "-C", str(clone),
            "--sandbox", self.sandbox,
            "--skip-git-repo-check",
            "--ephemeral",
            "--color", "never",
        ]
        if self.model:
            argv += ["-m", self.model]
        argv += self.extra_args
        argv.append(prompt)
        return argv

    def run(self, clone: Path, prompt: str, *, budget_seconds: int) -> ExecResult:
        if not self.codex_bin:
            return ExecResult(
                False,
                "codex CLI not found — install @openai/codex or set SHEPHERD_DEV_CODEX_CMD",
            )
        argv = self.build_argv(clone, prompt)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(clone),
                capture_output=True,
                text=True,
                timeout=max(30, budget_seconds),
                env={**os.environ, "CI": os.environ.get("CI", "1")},
            )
        except subprocess.TimeoutExpired:
            return ExecResult(
                False,
                f"codex worker timed out after {budget_seconds}s",
                round(time.monotonic() - started, 1),
            )
        except OSError as exc:
            return ExecResult(False, f"could not launch codex: {exc}", round(time.monotonic() - started, 1))
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-4000:]
        duration = round(time.monotonic() - started, 1)
        if proc.returncode != 0:
            return ExecResult(False, f"codex exited {proc.returncode}", duration, tail)
        return ExecResult(True, None, duration, tail)


def build_executor(
    *,
    codex_bin: str | None = None,
    model: str | None = None,
    fake_files: dict[str, bytes] | None = None,
) -> HostedExecutor:
    """Factory: explicit fake (tests) > env SHEPHERD_DEV_CODEX_FAKE=1 > real CLI."""
    if fake_files is not None:
        return FakeCodexExecutor(fake_files)
    if os.environ.get("SHEPHERD_DEV_CODEX_FAKE") == "1":
        return FakeCodexExecutor({"SHEPHERD_CODEX_FAKE.txt": b"fake\n"})
    return CliCodexExecutor(codex_bin=codex_bin, model=model)
