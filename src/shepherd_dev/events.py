"""Per-run event log + worker stream tailer (verbose mode core).

Three concerns, all best-effort — an observability failure must NEVER break or
slow a run:

- ``RunEventLog``: an append-only NDJSON log of normalized run events
  (``phase.*``, ``worker.*``, ``gate.*``, ``policy.*``, ``review.*``), one file
  per run under ``~/.shepherd-dev/runs/<run-id>/events.ndjson``. Live consumers
  subscribe as observers; post-hoc consumers read the file (``trace``).
- ``StreamTailer``: a thread that tails the worker's raw ``claude -p
  --output-format stream-json`` output (teed to a file by the supervisor's
  launch seam) and translates each tool call into normalized events — including
  the per-edit diff, recovered for free from the Edit tool's
  ``old_string``/``new_string`` input (no snapshots needed).
- ``parse_test_failure``: conservative per-framework parsers that turn a gate
  output line into a named failing test (the "each bug" feed).
"""

from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

HUNK_LIMIT = 4000
EXCERPT_LIMIT = 200

#: Where the launch seam tees the worker's raw stream-json, relative to the
#: jailed workspace. Lives inside the provider's scratch (the jail's only
#: housekeeping-writable root), which is scrubbed before the delta is captured
#: — so the tee can never leak into a retained proposal. The tailer must
#: therefore be drained before the scrub (see supervisor._TailingExecution).
TEE_RELPATH = Path(".claude-scratch") / "tmp" / "worker-stream.ndjson"


def _default_runs_root() -> Path:
    env = os.environ.get("SHEPHERD_DEV_RUNS_DIR")
    return Path(env) if env else Path.home() / ".shepherd-dev" / "runs"


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _excerpt(text: str, limit: int = EXCERPT_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class RunEventLog:
    """Append-only NDJSON event log for one run. Thread-safe; never raises."""

    def __init__(self, run_id: str | None = None, root: Path | None = None):
        self.run_id = run_id or new_run_id()
        self.root = Path(root) if root else _default_runs_root()
        self.dir = self.root / self.run_id
        self._seq = 0
        self._lock = threading.Lock()
        self._observers: list[Callable[[dict], None]] = []
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    @property
    def path(self) -> Path:
        return self.dir / "events.ndjson"

    def subscribe(self, observer: Callable[[dict], None]) -> None:
        self._observers.append(observer)

    def emit(self, kind: str, payload: dict | None = None, attempt: int | None = None) -> dict:
        """Append one event and notify observers. Best-effort on every step."""
        with self._lock:
            self._seq += 1
            event: dict = {"ts": round(time.time(), 3), "seq": self._seq, "kind": kind}
            if attempt is not None:
                event["attempt"] = attempt
            if payload:
                event["payload"] = payload
            try:
                line = json.dumps(event, ensure_ascii=False, default=str)
            except Exception:
                event["payload"] = {"repr": repr(payload)[:500]}
                line = json.dumps(event, ensure_ascii=False, default=str)
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass
        for observer in list(self._observers):
            try:
                observer(event)
            except Exception:
                pass
        return event


def load_run_events(run_id: str, root: Path | None = None) -> list[dict]:
    """Read one run's events (tolerant to bad lines). Empty list if absent."""
    path = (Path(root) if root else _default_runs_root()) / run_id / "events.ndjson"
    events: list[dict] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def latest_run_id(root: Path | None = None) -> str | None:
    """The most recent run id (ids sort chronologically by construction)."""
    base = Path(root) if root else _default_runs_root()
    try:
        ids = sorted(p.name for p in base.iterdir() if p.is_dir())
    except Exception:
        return None
    return ids[-1] if ids else None


def edit_hunk(old: str, new: str, path: str = "") -> dict:
    """A unified-diff hunk + added/removed line counts for one edit step."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    added = removed = 0
    parts: list[str] = []
    for line in difflib.unified_diff(old_lines, new_lines, fromfile=path, tofile=path, n=2):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
        parts.append(line if line.endswith("\n") else line + "\n")
    hunk = "".join(parts)
    if len(hunk) > HUNK_LIMIT:
        hunk = hunk[: HUNK_LIMIT - 1] + "…"
    return {"hunk": hunk, "add": added, "del": removed}


# -- gate output → named failing tests ---------------------------------------

_FAILURE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"^FAILED\s+(\S+::\S+)"), "pytest"),
    (re.compile(r"^(\S+::\S+)\s+FAILED\b"), "pytest"),
    (re.compile(r"^FAIL:\s+(\S+)\s+\("), "unittest"),
    (re.compile(r"^\s*[✕×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$"), "jest"),
    (re.compile(r"^\s*\d+\)\s+test\s+(.+\(\S+\))\s*$"), "exunit"),
    (re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED\s*$"), "cargo"),
    (re.compile(r"^--- FAIL:\s+(\S+)"), "go"),
)


def parse_test_failure(line: str) -> dict | None:
    """Name the failing test in one gate output line, or None. Conservative:
    a false negative only loses a verbose detail; a false positive lies."""
    line = line.rstrip("\n")
    if not line.strip():
        return None
    for pattern, framework in _FAILURE_PATTERNS:
        match = pattern.match(line)
        if match:
            return {"framework": framework, "test": match.group(1).strip()}
    return None


# -- worker stream-json → normalized events ----------------------------------

def _content_blocks(event: dict) -> list[dict]:
    """Content blocks of one claude stream event (same shapes the substrate
    parser accepts: message.content / content / blocks)."""
    message = event.get("message")
    candidates: list = []
    if isinstance(message, dict):
        candidates.append(message.get("content"))
    candidates.append(event.get("content"))
    candidates.append(event.get("blocks"))
    blocks: list[dict] = []
    for candidate in candidates:
        if isinstance(candidate, list):
            blocks.extend(b for b in candidate if isinstance(b, dict))
        elif isinstance(candidate, dict):
            blocks.append(candidate)
    return blocks


def _tool_target(params: dict) -> str:
    for key in ("file_path", "path", "pattern", "command", "url"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return _excerpt(value, 120)
    return ""


class StreamTailer(threading.Thread):
    """Tail the teed worker stream file, translating tool calls into events.

    The file may not exist yet at start (the tee is created inside the jail);
    the tailer polls until it appears. A partial trailing line is buffered
    until its newline arrives. Lines beyond ``max_line_bytes`` are flagged
    (``worker.raw``) and dropped, never parsed — a giant Write must not stall
    the run. ``drain()`` performs the final read and stops the thread; the
    supervisor calls it before the workspace scratch is scrubbed."""

    def __init__(
        self,
        path: Path | str,
        log: RunEventLog,
        *,
        read_baseline: Callable[[str], str | None] | None = None,
        poll_interval: float = 0.1,
        max_line_bytes: int = 2_000_000,
        attempt: int | None = None,
    ):
        super().__init__(daemon=True, name="shepherd-stream-tailer")
        self._path = Path(path)
        self._log = log
        self._read_baseline = read_baseline
        self._poll = poll_interval
        self._max = max_line_bytes
        self._attempt = attempt
        # NB: named _stopping, not _stop — Thread has an internal _stop()
        # method that join() calls on Python ≤3.12; shadowing it breaks join.
        self._stopping = threading.Event()
        self._buf = b""
        self._pos = 0

    # -- lifecycle ------------------------------------------------------------
    def run(self) -> None:
        while not self._stopping.wait(self._poll):
            self._pump()

    def drain(self, timeout: float = 2.0) -> None:
        """Stop the thread, then read whatever is left (incl. a final line
        without a newline). Safe to call more than once."""
        self._stopping.set()
        if self.is_alive():
            self.join(timeout)
        self._pump()
        if self._buf.strip():
            self._handle_line(self._buf)
        self._buf = b""

    # -- internals ------------------------------------------------------------
    def _pump(self) -> None:
        try:
            if not self._path.exists():
                return
            with open(self._path, "rb") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
            self._pos += len(chunk)
        except Exception:
            return
        if not chunk:
            return
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            self._handle_line(line)
        if len(self._buf) > self._max:  # a single line larger than the cap
            self._log.emit(
                "worker.raw", {"truncated": True, "bytes": len(self._buf)}, attempt=self._attempt
            )
            self._buf = b""

    def _handle_line(self, raw: bytes) -> None:
        raw = raw.strip()
        if not raw:
            return
        if len(raw) > self._max:
            self._log.emit(
                "worker.raw", {"truncated": True, "bytes": len(raw)}, attempt=self._attempt
            )
            return
        try:
            event = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return  # tail of a dropped oversize line, or CLI noise — skip
        if not isinstance(event, dict):
            return
        try:
            for block in _content_blocks(event):
                self._handle_block(block)
        except Exception:
            pass  # observability never breaks the run

    def _handle_block(self, block: dict) -> None:
        kind = block.get("type")
        if kind == "tool_use":
            name = str(block.get("name") or "tool")
            raw_params = block.get("input")
            params: dict = raw_params if isinstance(raw_params, dict) else {}
            self._log.emit(
                "worker.tool",
                {"tool": name, "target": _tool_target(params)},
                attempt=self._attempt,
            )
            if name == "Edit":
                self._emit_edit(params)
            elif name == "MultiEdit":
                edits = params.get("edits")
                path = params.get("file_path")
                if isinstance(edits, list):
                    for edit in edits:
                        if isinstance(edit, dict):
                            self._emit_edit({**edit, "file_path": path})
            elif name == "Write":
                self._emit_write(params)
        elif kind == "tool_result" and bool(block.get("is_error")):
            output = block.get("content")
            if isinstance(output, list):  # content-block list form
                output = " ".join(
                    str(b.get("text", "")) for b in output if isinstance(b, dict)
                )
            self._log.emit(
                "worker.tool.fail", {"error": _excerpt(str(output or ""))}, attempt=self._attempt
            )
        elif kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                self._log.emit("worker.note", {"text": _excerpt(text)}, attempt=self._attempt)

    def _emit_edit(self, params: dict) -> None:
        path = str(params.get("file_path") or "")
        old = params.get("old_string")
        new = params.get("new_string")
        if not (isinstance(old, str) and isinstance(new, str)):
            return
        payload = {"path": path, **edit_hunk(old, new, path=path)}
        self._log.emit("worker.edit", payload, attempt=self._attempt)

    def _emit_write(self, params: dict) -> None:
        path = str(params.get("file_path") or params.get("path") or "")
        content = params.get("content")
        if not isinstance(content, str):
            return
        payload: dict = {
            "path": path,
            "lines": len(content.splitlines()),
            "bytes": len(content.encode("utf-8", errors="replace")),
        }
        baseline = None
        if self._read_baseline is not None:
            try:
                baseline = self._read_baseline(path)
            except Exception:
                baseline = None
        if isinstance(baseline, str):
            payload.update(edit_hunk(baseline, content, path=path))
        self._log.emit("worker.write", payload, attempt=self._attempt)


class WorkerStreamHook:
    """Per-launch tailer factory handed to the supervisor's launch seam.

    The supervisor sets ``attempt`` before each worker launch; the seam calls
    ``start(working_path)`` just before the confined launch and ``drain(...)``
    right after it returns — before the provider scrubs the scratch that holds
    the tee file. Every method is failure-tolerant."""

    def __init__(
        self,
        log: RunEventLog,
        *,
        read_baseline: Callable[[str], str | None] | None = None,
    ):
        self.log = log
        self.read_baseline = read_baseline
        self.attempt: int | None = None

    def tee_path(self, working_path: Path | str) -> Path:
        return Path(working_path) / TEE_RELPATH

    def start(self, working_path: Path | str) -> StreamTailer:
        tailer = StreamTailer(
            self.tee_path(working_path),
            self.log,
            read_baseline=self.read_baseline,
            attempt=self.attempt,
        )
        tailer.start()
        return tailer

    def drain(self, tailer: StreamTailer, timeout: float = 2.0) -> None:
        try:
            tailer.drain(timeout)
        except Exception:
            pass
