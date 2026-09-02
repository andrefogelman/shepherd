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
            # Private by default: hunks and gate output can carry secrets (an
            # edited .env, a credential echoed by a failing test).
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            os.chmod(self.dir, 0o700)
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
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
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


def repo_baseline_reader(repo_root: Path) -> Callable[[str], str | None]:
    """A ``read_baseline`` for StreamTailer: resolve a worker tool path (often
    jail-absolute, pointing into the forked workspace) to the repo's CURRENT
    file content by longest existing suffix — so a Write can be rendered as a
    diff against the pre-change state. Traversal-safe and size-capped."""
    root = Path(repo_root).resolve()

    def read(path_str: str) -> str | None:
        if not path_str:
            return None
        parts = [p for p in Path(path_str).parts if p not in ("/", "")]
        for i in range(len(parts)):
            candidate = root.joinpath(*parts[i:])
            try:
                resolved = candidate.resolve()
                if not resolved.is_relative_to(root):
                    continue  # `..` or symlink escape — never read outside the repo
                if resolved.is_file() and resolved.stat().st_size <= 512 * 1024:
                    return resolved.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
        return None

    return read


def gate_line_observer(
    log: "RunEventLog",
    attempt: int | None = None,
    emit_lines: bool = True,
) -> Callable[[str], None]:
    """An ``on_line`` callback for the streaming gate: every line becomes a
    ``gate.line`` event (unless muted) and every recognized failure line also
    becomes a named ``gate.test.fail`` event."""

    def on_line(line: str) -> None:
        if emit_lines:
            log.emit("gate.line", {"line": _excerpt(line, 400)}, attempt=attempt)
        failure = parse_test_failure(line)
        if failure is not None:
            log.emit("gate.test.fail", failure, attempt=attempt)

    return on_line


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
        slot: dict | None = None,
    ):
        super().__init__(daemon=True, name="shepherd-stream-tailer")
        self._path = Path(path)
        self._log = log
        self._read_baseline = read_baseline
        self._poll = poll_interval
        self._max = max_line_bytes
        self._attempt = attempt
        #: Where the launch's telemetry lands for the thread that started
        #: this tailer (see WorkerStreamHook.take_result): the init event's
        #: model, and the result event's usage/cost/turns. A dict, not an
        #: attribute, because the tailer writes it from ITS thread and the
        #: supervisor reads it from the worker's.
        self._slot = slot if slot is not None else {}
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
            self._handle_envelope(event)
            for block in _content_blocks(event):
                self._handle_block(block)
        except Exception:
            pass  # observability never breaks the run

    def _handle_envelope(self, event: dict) -> None:
        """The two stream events that are about the SESSION rather than a
        turn: `system/init` (which model, which tools, which MCP servers —
        the hardening, as the CLI itself reports it) and `result` (tokens,
        cost, turns, wall and API time). Before this, both flowed through
        the tee and were dropped, so no run had a record of what it cost or
        what model produced it."""
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            info = session_init_info(event)
            self._slot["init"] = info
            self._log.emit("worker.init", info, attempt=self._attempt)
        elif kind == "result":
            info = session_result_info(event)
            if "model" not in info and isinstance(self._slot.get("init"), dict):
                model = self._slot["init"].get("model")
                if model:
                    info["model"] = model
            self._slot["result"] = info
            self._log.emit("worker.result", info, attempt=self._attempt)

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


_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def session_init_info(event: dict) -> dict:
    """What a `system/init` stream event says about the session. Names only."""
    servers = event.get("mcp_servers")
    names: list[str] = []
    if isinstance(servers, list):
        for item in servers:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
            elif isinstance(item, str):
                names.append(item)
    tools = event.get("tools")
    info: dict = {
        "model": str(event.get("model") or "") or None,
        "tools": len(tools) if isinstance(tools, list) else None,
        "mcp_servers": names,
    }
    version = event.get("claude_code_version")
    if version:
        info["cli_version"] = str(version)
    return info


def session_result_info(event: dict) -> dict:
    """What a `result` stream event says the session cost. Numbers only —
    the result text itself is not recorded here."""
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    info: dict = {}
    for key in _USAGE_KEYS:
        value = _int(usage.get(key))
        if value is not None:
            info[key] = value
    cost = _float(event.get("total_cost_usd"))
    if cost is not None:
        info["total_cost_usd"] = cost
    for key in ("num_turns", "duration_ms", "duration_api_ms"):
        value = _int(event.get(key))
        if value is not None:
            info[key] = value
    by_model = event.get("modelUsage")
    if isinstance(by_model, dict) and by_model:
        # The served model is the key; a session that fell back to another
        # model lists both, largest spend first is not guaranteed — keep them.
        info["models"] = sorted(str(k) for k in by_model)
        if len(by_model) == 1:
            info["model"] = next(iter(info["models"]))
    if event.get("is_error") is True:
        info["is_error"] = True
    subtype = event.get("subtype")
    if isinstance(subtype, str) and subtype:
        info["subtype"] = subtype
    return info


def format_usage(usage: dict | None) -> str:
    """One human line for a launch's telemetry, empty when there is none."""
    if not usage:
        return ""
    parts: list[str] = []
    tokens_in = usage.get("input_tokens")
    tokens_out = usage.get("output_tokens")
    if tokens_in is not None or tokens_out is not None:
        cached = usage.get("cache_read_input_tokens") or 0
        head = f"{tokens_in or 0:,} in / {tokens_out or 0:,} out"
        if cached:
            head += f" (+{cached:,} cached)"
        parts.append(head)
    if usage.get("num_turns") is not None:
        parts.append(f"{usage['num_turns']} turns")
    model = usage.get("model") or ", ".join(usage.get("models") or [])
    if model:
        parts.append(str(model))
    if usage.get("total_cost_usd") is not None:
        parts.append(f"${usage['total_cost_usd']:.2f}")
    return " · ".join(parts)


class WorkerStreamHook:
    """Per-launch tailer factory handed to the supervisor's launch seam.

    The supervisor sets ``attempt`` before each worker launch; the seam calls
    ``start(working_path)`` just before the confined launch and ``drain(...)``
    right after it returns — before the provider scrubs the scratch that holds
    the tee file. Every method is failure-tolerant.

    Parallel lanes (best-of) share ONE hook through the global transport seam
    but run each worker in its own thread: ``bind(log)`` routes that thread's
    launches to its candidate's log (thread-local override of the default).
    A thread with neither a bound nor a default log gets no tailer."""

    def __init__(
        self,
        log: RunEventLog | None = None,
        *,
        read_baseline: Callable[[str], str | None] | None = None,
    ):
        self.log = log
        self.read_baseline = read_baseline
        self.attempt: int | None = None
        self._local = threading.local()

    def bind(self, log: RunEventLog | None, attempt: int | None = None) -> None:
        """Route this thread's launches to ``log`` (parallel candidates)."""
        self._local.log = log
        self._local.attempt = attempt

    def set_attempt(self, attempt: int | None) -> None:
        """Update the attempt tag in whichever slot ``_current`` reads for this
        thread — the thread-local one when bound, else the shared default. A
        bare ``hook.attempt = n`` write would be ignored by bound threads."""
        if hasattr(self._local, "log"):
            self._local.attempt = attempt
        else:
            self.attempt = attempt

    def _current(self) -> tuple[RunEventLog | None, int | None]:
        if hasattr(self._local, "log"):
            return self._local.log, getattr(self._local, "attempt", None)
        return self.log, self.attempt

    def tee_path(self, working_path: Path | str) -> Path:
        return Path(working_path) / TEE_RELPATH

    def emit(self, kind: str, payload: dict | None = None) -> None:
        """Record an event on whichever log this thread's launches go to —
        the seam's way of noting something about a launch (the prompt it
        rendered, say) that is not a tool call. Silent when unbound."""
        log, attempt = self._current()
        if log is None:
            return
        try:
            log.emit(kind, payload, attempt=attempt)
        except Exception:
            pass

    def start(self, working_path: Path | str) -> StreamTailer | None:
        log, attempt = self._current()
        if log is None:
            return None
        # A fresh slot per launch, filed under THIS thread: the tailer fills
        # it from its own thread, and take_result() reads it from this one
        # once the launch returns.
        slot: dict = {}
        self._local.slot = slot
        tailer = StreamTailer(
            self.tee_path(working_path),
            log,
            read_baseline=self.read_baseline,
            attempt=attempt,
            slot=slot,
        )
        tailer.start()
        return tailer

    def take_result(self) -> dict | None:
        """The telemetry of the launch this thread most recently started —
        the `result` event's usage/cost/turns, with the init event's model
        folded in — and clear it, so a later launch on this thread cannot
        report the previous one's numbers. None when no result was seen (a
        killed launch, a thread that never launched, verbose off)."""
        slot = getattr(self._local, "slot", None)
        if not isinstance(slot, dict):
            return None
        result = slot.get("result")
        init = slot.get("init")
        self._local.slot = {}
        if not isinstance(result, dict):
            return None
        out = dict(result)
        if "model" not in out and isinstance(init, dict) and init.get("model"):
            out["model"] = init["model"]
        return out

    def drain(self, tailer: StreamTailer | None, timeout: float = 2.0) -> None:
        if tailer is None:
            return
        try:
            tailer.drain(timeout)
        except Exception:
            pass
