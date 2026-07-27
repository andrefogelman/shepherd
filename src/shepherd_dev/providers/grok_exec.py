"""Grok worker executors — run outside shepherd-ai, no Claude dependency.

Executors edit an isolated clone directory in place. The host layer then diffs
the clone against the original repo to build a proposal.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .hosted import ExecResult, HostedExecutor, run_cli_worker

# Back-compat: the executor protocol now lives in hosted (shared with codex).
GrokExecutor = HostedExecutor


def find_grok_bin(explicit: str | None = None) -> str | None:
    """Resolve the Grok CLI binary. Env SHEPHERD_DEV_GROK_CMD wins, then PATH."""
    if explicit:
        return explicit
    env = os.environ.get("SHEPHERD_DEV_GROK_CMD")
    if env:
        return env
    for name in ("grok",):
        found = shutil.which(name)
        if found:
            return found
    # Common install location for Grok Build TUI
    home = Path.home() / ".grok" / "bin" / "grok"
    if home.is_file() and os.access(home, os.X_OK):
        return str(home)
    return None


class FakeGrokExecutor:
    """Test/offline executor: applies a fixed set of file writes to the clone."""

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        fail: bool = False,
        error: str = "fake fail",
        on_run=None,
    ):
        self.files = files or {}
        self.fail = fail
        self.error = error
        #: Called with the clone once the "worker" starts — lets a test act on
        #: the real repo while the run is in flight (see #3).
        self.on_run = on_run

    def run(self, clone: Path, prompt: str, *, budget_seconds: int) -> ExecResult:
        started = time.monotonic()
        if self.on_run is not None:
            self.on_run(clone)
        if self.fail:
            return ExecResult(False, self.error, round(time.monotonic() - started, 1))
        for rel, content in self.files.items():
            target = (clone / rel).resolve()
            if not target.is_relative_to(clone.resolve()):
                return ExecResult(False, f"fake path escapes clone: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return ExecResult(True, None, round(time.monotonic() - started, 1), "fake executor wrote files")


class CliGrokExecutor:
    """Invoke the Grok Build CLI as a headless coding agent on `clone`."""

    def __init__(
        self,
        *,
        grok_bin: str | None = None,
        model: str | None = None,
        max_turns: int = 40,
        extra_args: list[str] | None = None,
    ):
        self.grok_bin = grok_bin or find_grok_bin()
        self.model = model or os.environ.get("SHEPHERD_DEV_GROK_MODEL")
        self.max_turns = max_turns
        self.extra_args = list(extra_args or [])

    def run(self, clone: Path, prompt: str, *, budget_seconds: int) -> ExecResult:
        if not self.grok_bin:
            return ExecResult(
                False,
                "grok CLI not found — install Grok Build TUI or set SHEPHERD_DEV_GROK_CMD",
            )
        # Prefer multi-turn agent with auto tool approval so files can be written.
        # Flags mirror the Grok Build CLI: --cwd, --always-approve, --permission-mode.
        argv = [
            self.grok_bin,
            "--cwd", str(clone),
            "--always-approve",
            "--permission-mode", "bypassPermissions",
            "--max-turns", str(self.max_turns),
            "--no-memory",
            "--output-format", "plain",
        ]
        if self.model:
            argv += ["--model", self.model]
        argv += self.extra_args
        argv.append(prompt)
        return run_cli_worker(argv, clone, budget_seconds=budget_seconds, label="grok")


def build_executor(
    *,
    grok_bin: str | None = None,
    model: str | None = None,
    fake_files: dict[str, bytes] | None = None,
) -> GrokExecutor:
    """Factory: explicit fake (tests) > env SHEPHERD_DEV_GROK_FAKE=1 > real CLI."""
    if fake_files is not None:
        return FakeGrokExecutor(fake_files)
    if os.environ.get("SHEPHERD_DEV_GROK_FAKE") == "1":
        # Offline smoke: write a sentinel the tests can override via env JSON path
        return FakeGrokExecutor({"SHEPHERD_GROK_FAKE.txt": b"fake\n"})
    return CliGrokExecutor(grok_bin=grok_bin, model=model)
