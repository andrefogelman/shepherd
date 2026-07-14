"""Grok worker executors — run outside shepherd-ai, no Claude dependency.

Executors edit an isolated clone directory in place. The host layer then diffs
the clone against the original repo to build a proposal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class ExecResult:
    ok: bool
    error: str | None = None
    duration_s: float | None = None
    output_tail: str = ""


class GrokExecutor(Protocol):
    def run(self, clone: Path, prompt: str, *, budget_seconds: int) -> ExecResult: ...


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
                f"grok worker timed out after {budget_seconds}s",
                round(time.monotonic() - started, 1),
            )
        except OSError as exc:
            return ExecResult(False, f"could not launch grok: {exc}", round(time.monotonic() - started, 1))
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-4000:]
        duration = round(time.monotonic() - started, 1)
        if proc.returncode != 0:
            return ExecResult(False, f"grok exited {proc.returncode}", duration, tail)
        return ExecResult(True, None, duration, tail)


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
