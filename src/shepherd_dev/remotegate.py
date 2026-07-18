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


def preflight(cfg: RemoteGateConfig, timeout: int = 20) -> str | None:
    """Verify the remote is usable BEFORE any worker runs. Returns an error
    string, or None when ready. Generic: only checks SSH + repo_dir + the test
    binary — nothing stack-specific."""
    binary = shlex.split(cfg.test_cmd)[0] if cfg.test_cmd.strip() else ""
    checks = [
        f"test -d {shlex.quote(cfg.repo_dir)} || {{ echo 'repo_dir missing: {cfg.repo_dir}' >&2; exit 3; }}",
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
    return None


def _tar_entries(entries: dict[str, bytes]) -> bytes:
    """Pack the proposal's changed files into a tar stream (for overlay)."""
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, content in entries.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            info.mtime = int(time.time())
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

    In a background thread it makes the ephemeral copy of the warm checkout and,
    for {id}-isolated configs only, runs setup (bringing up the per-run DB /
    container). Non-isolated setup touches SHARED external state and stays under
    run_remote_gate's serialization lock, so the warmup pre-copies but does not
    pre-setup it. When the worker finishes, run_remote_gate adopts this workdir
    and only overlays + tests, overlapping the copy/setup latency with worker time.

    Always teardown-safe: teardown() joins the staging thread first, so a warmup
    that is never consumed (worker produced nothing) leaves no orphan.
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

    def start(self) -> "GateWarmup":
        self._thread = threading.Thread(target=self._stage, daemon=True)
        self._thread.start()
        return self

    def _stage(self) -> None:
        try:
            copy = _remote(self.cfg, _build_copy_script(self.cfg, self.workdir), self.timeout)
            if copy.returncode != 0:
                self.error = f"warmup copy failed: {(copy.stderr or copy.stdout).strip()[-200:]}"
                return
            if self.cfg.setup_cmd and self.cfg.is_id_isolated:
                envp = _env_prefix(self.cfg, self.run_id, self.workdir)
                wd = shlex.quote(self.workdir)
                setup = _remote(self.cfg, f"cd {wd} && {envp}{_sub(self.cfg.setup_cmd, self.run_id, self.workdir)}", self.timeout)
                if setup.returncode != 0:
                    self.error = f"warmup setup failed: {((setup.stdout or '') + (setup.stderr or '')).strip()[-200:]}"
                    return
                self.did_setup = True
        except Exception as exc:  # never let a background failure escape
            self.error = f"warmup: {exc}"

    def join(self) -> None:
        if self._thread is not None:
            self._thread.join(self.timeout)

    def teardown(self) -> None:
        with self._td_lock:
            if self._torn:
                return
            self._torn = True
        self.join()  # staging must finish before we tear its state down
        _teardown_workdir(self.cfg, self.run_id, self.workdir, self.did_setup, self.timeout)


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

    # Adopt a healthy warmup; discard a failed one (clear its partial state).
    if warmup is not None:
        warmup.join()
    staged = warmup if (warmup is not None and warmup.error is None) else None
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
            test_line = f"cd {wd} && {envp}timeout {timeout} {_sub(cfg.test_cmd, run_id, workdir)}"
            from .procstream import run_streaming

            try:
                res = run_streaming(_remote_argv(cfg, test_line), timeout=timeout + 60, on_line=on_line)
            except OSError as exc:
                return GateResult(False, None, "", infra_error=f"could not run ssh: {exc}")
            if res.timed_out:
                return GateResult(False, None, "",
                    infra_error=f"remote test suite timed out after {timeout}s")
            tail = res.output[-4000:]
            # timeout(1) exits 124 on kill
            if res.returncode == 124:
                return GateResult(False, 124, tail, infra_error=f"remote test timed out after {timeout}s")
            return GateResult(passed=res.returncode == 0, exit_code=res.returncode, output_tail=tail)
        finally:
            # 5. guaranteed teardown + cleanup — even on timeout/error
            _teardown_workdir(cfg, run_id, workdir, did_setup, timeout)
    finally:
        if lock:
            lock.release()
