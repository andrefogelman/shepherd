"""Remote test gate: run the gate on an arbitrary host over SSH.

For repos whose build/test only works in an environment the local sandbox
lacks — a database, a container, another architecture, a GPU — the worker
still runs locally (it only edits files), but the gate runs on a host that
has the environment.

Fully generic: shepherd knows nothing about any database, service, or
toolchain. The user declares it entirely via config (test_remote):

    {
      "test_remote": {
        "ssh": "user@host",                 # any SSH target/alias
        "repo_dir": "/path/to/warm/checkout",  # deps/build already compiled there
        "test_cmd": "<the gate command>",
        "setup_cmd": "<optional: bring up DB/containers/services>",
        "teardown_cmd": "<optional: tear them down — ALWAYS runs>",
        "writable": ["_build"],             # dirs the test writes to (unshared copy)
        "env": {"MIX_ENV": "test", "DATABASE_URL": "…{id}…"},
        "workdir_base": "/tmp/shepherd-gate",
        "ssh_opts": ["-o", "…"]
      }
    }

Every command and env value may reference {id} (a unique per-gate-run token)
and {workdir} (the remote ephemeral copy). That is how isolation for stateful
services works without shepherd knowing the service: name a per-{id} database /
compose project / container. There is NO service-specific code here.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from io import BytesIO

# Serializes stateful remote gates whose config is not {id}-isolated, so
# parallel modes (run2/best-of) can't corrupt shared external state.
_REMOTE_GATE_LOCK = threading.Lock()

# Writable-dir defaults per language (dirs the test writes into, which must be
# a real copy — not a hardlink — so the warm checkout is never mutated).
DEFAULT_WRITABLE = {
    "elixir": ["_build"],
    "rust": ["target"],
    "js": [],
    "python": [],
    "go": [],
}


@dataclass
class RemoteGateConfig:
    ssh: str
    repo_dir: str
    test_cmd: str
    setup_cmd: str | None = None
    teardown_cmd: str | None = None
    writable: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    workdir_base: str = "/tmp/shepherd-gate"
    ssh_opts: list[str] = field(default_factory=list)
    # How to make the ephemeral copy of the warm checkout. Default is GNU cp
    # hardlink-copy (instant, Linux — the common remote-host OS). Override with
    # {repo}/{workdir} placeholders for BSD/macOS hosts, e.g.
    # "rsync -a --link-dest={repo} {repo}/ {workdir}/".
    copy_cmd: str = "cp -al {repo} {workdir}"

    @property
    def is_id_isolated(self) -> bool:
        """A config that references {id} in setup/teardown/env isolates its own
        external state per run — parallel gates then need no serialization."""
        blobs = [self.setup_cmd or "", self.teardown_cmd or "", *self.env.values()]
        return any("{id}" in b for b in blobs)


def parse_remote_config(raw: dict, language: str | None) -> RemoteGateConfig | None:
    """Build a RemoteGateConfig from the repo's test_remote block, or None."""
    if not isinstance(raw, dict) or not raw.get("ssh") or not raw.get("repo_dir"):
        return None
    writable = raw.get("writable")
    if writable is None:
        writable = DEFAULT_WRITABLE.get(language or "", [])
    return RemoteGateConfig(
        ssh=str(raw["ssh"]),
        repo_dir=str(raw["repo_dir"]).rstrip("/"),
        test_cmd=str(raw.get("test_cmd") or ""),
        setup_cmd=raw.get("setup_cmd"),
        teardown_cmd=raw.get("teardown_cmd"),
        writable=[str(w) for w in writable],
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        workdir_base=str(raw.get("workdir_base") or "/tmp/shepherd-gate").rstrip("/"),
        ssh_opts=[str(o) for o in (raw.get("ssh_opts") or [])],
        copy_cmd=str(raw.get("copy_cmd") or "cp -al {repo} {workdir}"),
    )


def _ssh_base(cfg: RemoteGateConfig) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", *cfg.ssh_opts, cfg.ssh]


def _remote_argv(cfg: RemoteGateConfig, script: str) -> list[str]:
    """argv for running `script` on the remote via ssh.

    CRITICAL: ssh concatenates every arg after the host into ONE remote command
    string, which the remote login shell then re-tokenizes. So the whole
    `bash -lc <script>` must be a SINGLE ssh argument, with the script shell-
    quoted, or the remote shell splits the script on its own spaces and hands
    bash -c only the first word. Passing ["bash","-lc",script] as separate argv
    is the classic bug — it looks right locally but breaks over real ssh."""
    return [*_ssh_base(cfg), f"bash -lc {shlex.quote(script)}"]


def _sub(text: str, run_id: str, workdir: str) -> str:
    return text.replace("{id}", run_id).replace("{workdir}", workdir)


def _remote(cfg: RemoteGateConfig, script: str, timeout: int) -> subprocess.CompletedProcess:
    """Run a shell script on the remote via a single ssh invocation."""
    return subprocess.run(
        _remote_argv(cfg, script),
        capture_output=True, text=True, timeout=timeout,
    )


def _command_word(test_cmd: str) -> str:
    """The binary a test_cmd actually invokes, looking past any leading VAR=…
    assignments. `MIX_ENV=test mix test` runs `mix`, not `MIX_ENV=test` — taking
    argv[0] blindly made preflight demand a binary literally named after the
    assignment, which can never resolve, so a valid remote config was rejected
    before any worker ran (#9). Returns "" when there is nothing to check."""
    try:
        parts = shlex.split(test_cmd)
    except ValueError:  # unbalanced quotes — leave it to the remote shell
        return ""
    for part in parts:
        # An assignment only counts as a prefix while no command word has been
        # seen: `mix test FOO=bar` invokes mix, and `=x` / `1=x` are not valid
        # assignments, so they ARE the command word.
        name, sep, _ = part.partition("=")
        if sep and name.isidentifier():
            continue
        return part
    return ""


def preflight(cfg: RemoteGateConfig, timeout: int = 20) -> str | None:
    """Verify the remote is usable BEFORE any worker runs. Returns an error
    string, or None when ready. Generic: only checks SSH + repo_dir + the test
    binary — nothing stack-specific."""
    binary = _command_word(cfg.test_cmd)
    checks = [
        f"test -d {shlex.quote(cfg.repo_dir)} || {{ echo 'repo_dir missing: {cfg.repo_dir}' >&2; exit 3; }}",
        # Not fatal: the gate falls back to running the suite bare. But say so —
        # without a remote `timeout` the budget is enforced only by the local
        # ssh deadline, so a hung suite is reaped later and from this side.
        "command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1 || "
        "echo 'SHEPHERD_NO_REMOTE_TIMEOUT' >&2",
    ]
    if binary and "/" not in binary and "{" not in binary:
        checks.append(
            f"command -v {shlex.quote(binary)} >/dev/null || "
            f"{{ echo 'test binary not found on remote: {binary}' >&2; exit 4; }}"
        )
    try:
        proc = _remote(cfg, " && ".join(checks), timeout)
    except subprocess.TimeoutExpired:
        return f"remote preflight: ssh to {cfg.ssh} timed out after {timeout}s"
    except OSError as exc:
        return f"remote preflight: could not run ssh ({exc})"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return f"remote preflight failed (ssh {cfg.ssh}): {detail or f'exit {proc.returncode}'}"
    if "SHEPHERD_NO_REMOTE_TIMEOUT" in (proc.stderr or ""):
        print(
            f"remote gate: no timeout(1) on {cfg.ssh} — the suite will run without "
            f"a remote kill, so a hung one is reaped by the local ssh deadline "
            f"instead (later, and from this side). Install coreutils to tighten it.",
            file=sys.stderr,
        )
    return None


def _build_test_line(wd: str, envp: str, remote_cmd: str, timeout: int) -> str:
    """The remote gate's test step, with its own kill where one is available.

    `timeout` is GNU coreutils: absent on macOS and on minimal images.
    Invoking it unconditionally made every gate on such a host exit 127 with
    `timeout: command not found`, reported as an ordinary suite failure. Prefer
    it, then `gtimeout` (coreutils under its Homebrew name), then run bare.

    Losing the remote-side kill is not losing the budget: run_remote_gate's
    LOCAL deadline still reaps the ssh and its process group. What the remote
    `timeout` adds is killing the suite ON the remote host rather than leaving
    it to whatever the severed ssh session takes down with it.
    """
    return (
        f"cd {wd} && {envp}"
        f"if command -v timeout >/dev/null 2>&1; then "
        f"timeout {timeout} {remote_cmd}; "
        f"elif command -v gtimeout >/dev/null 2>&1; then "
        f"gtimeout {timeout} {remote_cmd}; "
        f"else {remote_cmd}; fi"
    )


def _tar_entries(entries: dict[str, bytes]) -> bytes:
    """Pack the proposal's changed files into a tar stream (for overlay).

    Member modes come from the entries' `.executable` set: the remote side
    untars and runs test_cmd against the result, so an exec bit dropped here
    fails `./script.sh` gates BEFORE the proposal is ever judged — the same
    loss materialize_into's chmod prevents locally.
    """
    executable = getattr(entries, "executable", frozenset())
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, content in entries.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            info.mtime = int(time.time())
            info.mode = 0o755 if rel in executable else 0o644
            tar.addfile(info, BytesIO(content))
    return buf.getvalue()


def _is_safe_rel(rel: str) -> bool:
    """A proposal path that stays inside the workdir — relative, no `..`, no
    absolute/home. Defense-in-depth: the tar/rm run on the remote host, so an
    unsanitized `..` would write/delete outside the ephemeral copy (tar-slip)."""
    from pathlib import PurePosixPath

    if rel.startswith(("/", "~", "\\")):
        return False
    p = PurePosixPath(rel)
    return not p.is_absolute() and ".." not in p.parts


def _overlay(cfg: RemoteGateConfig, workdir: str, entries: dict[str, bytes], timeout: int) -> str | None:
    """Overlay the proposal's files onto the ephemeral copy with remove-then-write
    semantics (break the hardlink so the warm checkout is never mutated)."""
    unsafe = sorted(r for r in entries if not _is_safe_rel(r))
    if unsafe:
        return f"overlay refused unsafe path(s) (escape the workdir): {unsafe}"
    quoted = " ".join(shlex.quote(rel) for rel in entries)
    unlink = f"cd {shlex.quote(workdir)} && for f in {quoted}; do rm -f \"$f\"; done" if entries else "true"
    script = f"{unlink} && mkdir -p {shlex.quote(workdir)} && tar -xf - -C {shlex.quote(workdir)}"
    try:
        proc = subprocess.run(
            _remote_argv(cfg, script),
            input=_tar_entries(entries), capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "overlay: timed out sending proposal to remote"
    except OSError as exc:
        return f"overlay: {exc}"
    if proc.returncode != 0:
        return f"overlay failed: {(proc.stderr or b'').decode(errors='replace')[-300:]}"
    return None


def _build_copy_script(cfg: RemoteGateConfig, workdir: str) -> str:
    """Mirror the warm checkout (cheap copy via copy_cmd, hardlinks by default),
    then a REAL copy of each writable dir so the test can write to it without
    touching the warm original."""
    repo = shlex.quote(cfg.repo_dir)
    wd = shlex.quote(workdir)
    copy = cfg.copy_cmd.replace("{repo}", repo).replace("{workdir}", wd)
    lines = [f"rm -rf {wd} && {copy}"]
    for w in cfg.writable:
        wq = shlex.quote(w)
        lines.append(f"if [ -e {repo}/{wq} ]; then rm -rf {wd}/{wq} && cp -a {repo}/{wq} {wd}/{wq}; fi")
    return " && ".join(lines)


def _env_prefix(cfg: RemoteGateConfig, run_id: str, workdir: str) -> str:
    if not cfg.env:
        return ""
    parts = [f"{k}={shlex.quote(_sub(v, run_id, workdir))}" for k, v in cfg.env.items()]
    return "export " + " ".join(parts) + "; "


def _teardown_workdir(cfg: RemoteGateConfig, run_id: str, workdir: str, did_setup: bool, timeout: int) -> None:
    """Best-effort remote teardown of a staged/used workdir (+ setup state)."""
    envp = _env_prefix(cfg, run_id, workdir)
    wd = shlex.quote(workdir)
    fin = []
    if did_setup and cfg.teardown_cmd:
        fin.append(f"cd {wd} 2>/dev/null && {envp}{_sub(cfg.teardown_cmd, run_id, workdir)} || true")
    fin.append(f"rm -rf {wd} || true")
    try:
        _remote(cfg, "; ".join(fin), timeout)
    except Exception:
        pass


class GateWarmup:
    """Speculatively pre-stages a remote gate workdir while the worker runs (#2).

    In a background thread it makes the ephemeral copy of the warm checkout —
    that alone, never setup_cmd, regardless of {id}-isolation. The workdir is
    always uniquely named (sg-<run_id>), so the copy can never collide with
    anything else on the remote host. setup_cmd is a different matter: it is
    arbitrary, user-supplied, and typically provisions a NAMED resource (a
    container, a compose project) in the same namespace the worker's own task
    instructions may independently touch on that same remote host, for the
    whole duration of the worker's run — shepherd's sandbox confines the
    worker's local writes, not its network reach. Running setup_cmd here,
    concurrently with an actor shepherd does not control, is exactly the
    window a same-named-resource race needs; run_remote_gate runs it instead,
    strictly after the worker has returned. When the worker finishes,
    run_remote_gate adopts this workdir and overlays + sets up + tests.

    Always teardown-safe: teardown() waits for the staging thread before tearing
    its state down, so a warmup that is never consumed (worker produced nothing)
    leaves no orphan.

    Adoption is gated on COMPLETION, not on the absence of an error (#1). _stage
    makes up to two remote calls of `timeout` each, so the thread's worst case is
    twice what a single-timeout join allows for; a join that gave up mid-copy
    left `error` at None, which the caller read as "staged and healthy" — and the
    gate then overlaid a half-copied tree and re-ran setup alongside the warmup's
    own still-running one. join() now honours the real staging budget and reports
    whether staging actually finished.
    """

    def __init__(self, cfg: RemoteGateConfig, timeout: int = 600):
        self.cfg = cfg
        self.timeout = timeout
        self.run_id = uuid.uuid4().hex[:12]
        self.workdir = f"{cfg.workdir_base}/sg-{self.run_id}"
        self.did_setup = False
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._torn = False
        self._td_lock = threading.Lock()
        self._done = threading.Event()
        #: Absolute monotonic point past which staging cannot legitimately still
        #: be running: two remote calls of `timeout` each, plus ssh slack.
        self._deadline: float | None = None

    def start(self) -> "GateWarmup":
        self._deadline = time.monotonic() + 2 * self.timeout + 5
        self._thread = threading.Thread(target=self._stage, daemon=True)
        self._thread.start()
        return self

    def _stage(self) -> None:
        try:
            copy = _remote(self.cfg, _build_copy_script(self.cfg, self.workdir), self.timeout)
            if copy.returncode != 0:
                self.error = f"warmup copy failed: {(copy.stderr or copy.stdout).strip()[-200:]}"
                return
            # setup_cmd deliberately does NOT run here — see the class docstring.
        except Exception as exc:  # never let a background failure escape
            self.error = f"warmup: {exc}"
        finally:
            self._done.set()

    @property
    def completed(self) -> bool:
        """True once _stage has run to its end — success OR recorded failure."""
        return self._done.is_set()

    def join(self) -> bool:
        """Wait out the staging budget. False = staging is STILL RUNNING, in
        which case neither the workdir nor `did_setup` describes anything the
        caller may rely on."""
        if self._thread is None:
            return self._done.is_set()
        remaining = 0.0 if self._deadline is None else max(0.0, self._deadline - time.monotonic())
        self._done.wait(remaining)
        return self._done.is_set()

    def teardown(self) -> None:
        with self._td_lock:
            if self._torn:
                return
            self._torn = True
        if not self._done.is_set():
            # Staging still owns the workdir; removing it now would race the ssh
            # calls writing into it, and blocking here would stall the gate for
            # the whole budget. Reap behind the caller — the run_id is ours
            # alone, so a fresh gate attempt cannot collide with it.
            threading.Thread(target=self._reap, daemon=True).start()
            return
        self._reap()

    def _reap(self) -> None:
        if self._deadline is not None:
            self._done.wait(max(0.0, self._deadline - time.monotonic()))
        _teardown_workdir(self.cfg, self.run_id, self.workdir, self.did_setup, self.timeout)


def _adoptable(warmup: "GateWarmup | None") -> bool:
    """A warmup may be adopted only once staging has FINISHED and reported no
    error. `error is None` alone is also true of a warmup that is merely still
    running (#1)."""
    return warmup is not None and warmup.completed and warmup.error is None


def run_remote_gate(
    cfg: RemoteGateConfig,
    entries: dict[str, bytes],
    timeout: int,
    warmup: "GateWarmup | None" = None,
    on_line=None,
):
    """Run one gate attempt remotely. Returns a GateResult (imported lazily to
    avoid a cycle). Guarantees teardown + cleanup of the ephemeral workdir.

    If a warmup is passed, run_remote_gate OWNS it: it adopts the pre-staged
    workdir (skipping the copy, and setup when the warmup already did it) or, if
    the warmup failed, tears its partial state down and proceeds fresh.
    ``on_line`` (verbose mode) receives each merged output line of the remote
    test step as ssh delivers it; copy/setup/teardown stay unstreamed."""
    from .supervisor import GateResult

    if not cfg.test_cmd.strip():
        if warmup is not None:
            warmup.teardown()
        return GateResult(False, 1, "remote gate: no test_cmd configured")

    # Adopt a COMPLETED, healthy warmup; discard a failed or still-staging one
    # (clearing its partial state). Never adopt on `error is None` alone — that
    # is also true of staging that simply has not finished (#1).
    if warmup is not None:
        warmup.join()
    staged = warmup if _adoptable(warmup) else None
    if warmup is not None and staged is None:
        warmup.teardown()

    serialize = not cfg.is_id_isolated
    lock = _REMOTE_GATE_LOCK if serialize else None
    if lock:
        lock.acquire()
    try:
        if staged is not None:
            run_id, workdir, did_setup = staged.run_id, staged.workdir, staged.did_setup
            staged._torn = True  # this call now owns teardown
        else:
            run_id = uuid.uuid4().hex[:12]
            workdir = f"{cfg.workdir_base}/sg-{run_id}"
            did_setup = False
        envp = _env_prefix(cfg, run_id, workdir)
        wd = shlex.quote(workdir)
        try:
            # 1. ephemeral copy of the warm checkout (skipped when pre-staged)
            if staged is None:
                copy = _remote(cfg, _build_copy_script(cfg, workdir), timeout)
                if copy.returncode != 0:
                    return GateResult(False, None, "",
                        infra_error=f"remote copy failed: {(copy.stderr or copy.stdout).strip()[-300:]}")

            # 2. overlay the proposal's files (remove-then-write)
            err = _overlay(cfg, workdir, entries, timeout)
            if err:
                return GateResult(False, None, "", infra_error=err)

            # 3. setup (bring up DB/containers/services — skipped when pre-staged)
            if cfg.setup_cmd and not did_setup:
                did_setup = True
                setup = _remote(cfg, f"cd {wd} && {envp}{_sub(cfg.setup_cmd, run_id, workdir)}", timeout)
                if setup.returncode != 0:
                    tail = ((setup.stdout or "") + "\n" + (setup.stderr or "")).strip()[-1500:]
                    return GateResult(False, None, "",
                        infra_error=f"remote setup_cmd failed (exit {setup.returncode}): {tail}")

            # 4. the gate itself, with a remote timeout so a hung test is killed
            # remotely. Streamed line by line so verbose mode sees each test as
            # it runs; the local deadline still reaps a hung ssh (process group).
            test_line = _build_test_line(
                wd, envp, _sub(cfg.test_cmd, run_id, workdir), timeout
            )
            from .procstream import run_streaming

            started = time.monotonic()
            try:
                res = run_streaming(_remote_argv(cfg, test_line), timeout=timeout + 60, on_line=on_line)
            except OSError as exc:
                return GateResult(False, None, "", infra_error=f"could not run ssh: {exc}")
            elapsed = time.monotonic() - started
            if res.timed_out:
                return GateResult(False, None, "",
                    infra_error=f"remote test suite timed out after {timeout}s")
            tail = res.output[-4000:]
            # timeout(1) exits 124 on kill — but so does a suite that simply
            # exits 124 (a runner reporting a failure count, say), and timeout
            # propagates the command's own status verbatim, so the code alone
            # cannot tell them apart. Elapsed time can: a killed command ran the
            # whole budget, an early 124 is the suite's own verdict. Reading
            # every 124 as a timeout turned an ordinary, RETRYABLE gate failure
            # into an infra_error that aborts the entire run (#10).
            if res.returncode == 124 and elapsed >= timeout * 0.95:
                return GateResult(False, 124, tail, infra_error=f"remote test timed out after {timeout}s")
            return GateResult(passed=res.returncode == 0, exit_code=res.returncode, output_tail=tail)
        finally:
            # 5. guaranteed teardown + cleanup — even on timeout/error
            _teardown_workdir(cfg, run_id, workdir, did_setup, timeout)
    finally:
        if lock:
            lock.release()
