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


def _sub(text: str, run_id: str, workdir: str) -> str:
    return text.replace("{id}", run_id).replace("{workdir}", workdir)


def _remote(cfg: RemoteGateConfig, script: str, timeout: int) -> subprocess.CompletedProcess:
    """Run a shell script on the remote via a single ssh invocation."""
    return subprocess.run(
        [*_ssh_base(cfg), "bash", "-lc", script],
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


def _overlay(cfg: RemoteGateConfig, workdir: str, entries: dict[str, bytes], timeout: int) -> str | None:
    """Overlay the proposal's files onto the ephemeral copy with remove-then-write
    semantics (break the hardlink so the warm checkout is never mutated)."""
    quoted = " ".join(shlex.quote(rel) for rel in entries)
    unlink = f"cd {shlex.quote(workdir)} && for f in {quoted}; do rm -f \"$f\"; done" if entries else "true"
    try:
        proc = subprocess.run(
            [*_ssh_base(cfg), "bash", "-lc",
             f"{unlink} && mkdir -p {shlex.quote(workdir)} && tar -xf - -C {shlex.quote(workdir)}"],
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


def run_remote_gate(cfg: RemoteGateConfig, entries: dict[str, bytes], timeout: int):
    """Run one gate attempt remotely. Returns a GateResult (imported lazily to
    avoid a cycle). Guarantees teardown + cleanup of the ephemeral workdir."""
    from .supervisor import GateResult

    if not cfg.test_cmd.strip():
        return GateResult(False, 1, "remote gate: no test_cmd configured")

    serialize = not cfg.is_id_isolated
    lock = _REMOTE_GATE_LOCK if serialize else None
    if lock:
        lock.acquire()
    try:
        run_id = uuid.uuid4().hex[:12]
        workdir = f"{cfg.workdir_base}/sg-{run_id}"
        envp = _env_prefix(cfg, run_id, workdir)
        wd = shlex.quote(workdir)
        did_setup = False
        try:
            # 1. ephemeral copy of the warm checkout (+ real copy of writable dirs)
            copy = _remote(cfg, _build_copy_script(cfg, workdir), timeout)
            if copy.returncode != 0:
                return GateResult(False, None, "",
                    infra_error=f"remote copy failed: {(copy.stderr or copy.stdout).strip()[-300:]}")

            # 2. overlay the proposal's files (remove-then-write)
            err = _overlay(cfg, workdir, entries, timeout)
            if err:
                return GateResult(False, None, "", infra_error=err)

            # 3. setup (bring up DB/containers/services — user's command)
            if cfg.setup_cmd:
                did_setup = True
                setup = _remote(cfg, f"cd {wd} && {envp}{_sub(cfg.setup_cmd, run_id, workdir)}", timeout)
                if setup.returncode != 0:
                    tail = ((setup.stdout or "") + "\n" + (setup.stderr or "")).strip()[-1500:]
                    return GateResult(False, None, "",
                        infra_error=f"remote setup_cmd failed (exit {setup.returncode}): {tail}")

            # 4. the gate itself, with a remote timeout so a hung test is killed remotely
            test_line = f"cd {wd} && {envp}timeout {timeout} {_sub(cfg.test_cmd, run_id, workdir)}"
            try:
                proc = _remote(cfg, test_line, timeout + 60)
            except subprocess.TimeoutExpired:
                return GateResult(False, None, "",
                    infra_error=f"remote test suite timed out after {timeout}s")
            tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-4000:]
            # timeout(1) exits 124 on kill
            if proc.returncode == 124:
                return GateResult(False, 124, tail, infra_error=f"remote test timed out after {timeout}s")
            return GateResult(passed=proc.returncode == 0, exit_code=proc.returncode, output_tail=tail)
        finally:
            # 5. guaranteed teardown + cleanup — even on timeout/error
            fin = []
            if did_setup and cfg.teardown_cmd:
                fin.append(f"cd {wd} 2>/dev/null && {envp}{_sub(cfg.teardown_cmd, run_id, workdir)} || true")
            fin.append(f"rm -rf {wd} || true")
            try:
                _remote(cfg, "; ".join(fin), timeout)
            except Exception:
                pass
    finally:
        if lock:
            lock.release()
