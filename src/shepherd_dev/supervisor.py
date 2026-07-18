"""Supervisor meta-agent: runs a worker task in a sandbox, applies the
changeset policy, gates the retained output on the repo's test suite, and
retries with injected guidance on failure.

The supervisor NEVER applies anything to the workspace. A passing attempt
stays retained; settlement (select/apply/discard) is always a human decision.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .policy import ChangesetPolicy, check_paths

IGNORED_DIRS = {
    ".vcscore",
    ".venv",
    "node_modules",
    "__pycache__",
    ".shepherd",
    ".review",
    ".shepherd-proposals",
}
DIFF_TEXT_LIMIT = 60_000  # chars of proposal content handed to the reviewer


@dataclass
class GateResult:
    passed: bool
    exit_code: int | None
    output_tail: str
    infra_error: str | None = None  # suite could not run at all


@dataclass
class Attempt:
    number: int
    run_ref: str
    changed_paths: list[str]
    policy_violations: list[str]
    gate: GateResult | None
    verdict: str  # run_failed | no_change | policy_rejected | tests_failed | passed | timed_out
    error: str | None = None
    duration_s: float | None = None  # worker wall-clock (cost/speed telemetry)


@dataclass
class ReviewVerdict:
    approved: bool
    summary: str
    issues: list[str] = field(default_factory=list)
    error: str | None = None  # review ran but verdict could not be obtained


@dataclass
class DevReport:
    feature: str
    succeeded: bool
    attempts: list[Attempt] = field(default_factory=list)
    final_run_ref: str | None = None
    settlement_hint: str | None = None
    review: ReviewVerdict | None = None
    repo: str = ""
    # content entries of the passing proposal (set on success) — consumed by
    # the parallel coordinator; per-file bytes, small by policy cap
    entries: dict[str, bytes] | None = None

    def summary(self) -> str:
        lines = [f"feature: {self.feature}", f"succeeded: {self.succeeded}"]
        for a in self.attempts:
            lines.append(
                f"  attempt {a.number}: run={a.run_ref} verdict={a.verdict} "
                f"changed={len(a.changed_paths)}"
            )
            if a.error:
                lines.append(f"    error: {a.error}")
            if a.policy_violations:
                lines += [f"    policy: {v}" for v in a.policy_violations]
            if a.gate and not a.gate.passed:
                reason = a.gate.infra_error or a.gate.output_tail[-500:]
                lines.append(f"    gate: exit={a.gate.exit_code} {reason}")
        if self.review:
            if self.review.error:
                lines.append(f"review: UNAVAILABLE ({self.review.error})")
            else:
                lines.append(f"review: {'APPROVED' if self.review.approved else 'REJECTED'} — {self.review.summary}")
                lines += [f"  issue: {i}" for i in self.review.issues]
        if self.final_run_ref:
            repo_arg = f" --repo {self.repo}" if self.repo else ""
            lines += [
                "",
                "retained for human settlement:",
                f"  shepherd run changeset {self.final_run_ref}                # inspect",
                f"  shepherd-dev settle {self.final_run_ref}{repo_arg}           # accept: advance world + write files",
                f"  shepherd-dev settle {self.final_run_ref}{repo_arg} --reject  # discard proposal",
            ]
        return "\n".join(lines)


def materialize_into(root: Path, entries: dict[str, bytes]) -> list[str]:
    """Write changeset content entries under root.

    Refuses paths that escape root. Returns the list of written paths.
    """
    written: list[str] = []
    resolved_root = root.resolve()
    for rel, content in entries.items():
        target = (root / rel).resolve()
        if not target.is_relative_to(resolved_root):
            raise ValueError(f"changeset path escapes repo root: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written.append(rel)
    return written


def read_changeset_entries(changeset) -> dict[str, bytes]:
    """Snapshot a retained changeset's content entries into memory.

    v0.3.0 lane reality (verified on 3 workspaces): runs fork from the
    workspace's ORIGINAL adoption basis, not from later settlements, so
    changed_paths can list stale-basis artifacts whose content is
    unavailable (read_file -> None). Those are NOT worker actions; the
    git worktree is our source of truth, so they are skipped. Consequence:
    genuine worker deletions cannot be expressed in this lane (documented
    limitation; effect-stream support in F3).
    """
    entries: dict[str, bytes] = {}
    for rel in changeset.changed_paths:
        entry = changeset.read_file(rel)  # (bytes, mode) | None
        if entry is not None:
            entries[rel] = entry[0]
    return entries


def fast_copytree(src: Path, dest: Path, ignored: set[str] | None = None) -> None:
    """Tree copy tuned for the gate/clone hot path: per top-level entry, try
    the filesystem's cheap copy (`cp -c` clonefile on APFS, `cp -R` elsewhere
    — measured 3.5× faster than shutil.copytree on a 1500-file tree) and fall
    back to shutil.copytree per entry. ``ignored`` names are skipped at the
    top level (where .git/.venv/node_modules live)."""
    import subprocess

    ignored = ignored or set()
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(Path(src).iterdir(), key=lambda p: p.name):
        if entry.name in ignored:
            continue
        target = dest / entry.name
        done = False
        for argv in (["cp", "-c", "-R", str(entry), str(target)],
                     ["cp", "-R", str(entry), str(target)]):
            try:
                if subprocess.run(argv, capture_output=True).returncode == 0:
                    done = True
                    break
            except Exception:
                pass
        if not done:  # last resort: pure-python copy
            if entry.is_dir():
                shutil.copytree(entry, target, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, target, follow_symlinks=False)


def _materialize(repo_root: Path, entries: dict[str, bytes], dest: Path) -> None:
    """Copy the repo and overlay the proposal's content entries on top."""
    fast_copytree(Path(repo_root), dest, ignored=set(IGNORED_DIRS))
    materialize_into(dest, entries)


class LocalGateStage:
    """Pre-staged pristine repo copy for the LOCAL gate (the local analogue of
    the remote GateWarmup): built once in the background while the worker
    runs; each gate attempt clonefiles the pristine base and overlays only the
    proposal's entries — the per-attempt cost drops from a full tree copy to
    a metadata clone. Failure-tolerant: any error makes stage() return None
    and the gate falls back to the ordinary full materialize."""

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self.base: Path | None = None
        self.error: str | None = None
        self._root = Path(tempfile.mkdtemp(prefix="shepherd-gatestage-"))
        self._thread: threading.Thread | None = None
        self._n = 0

    def start(self) -> "LocalGateStage":
        self._thread = threading.Thread(target=self._build, daemon=True, name="shepherd-gate-stage")
        self._thread.start()
        return self

    def _build(self) -> None:
        try:
            base = self._root / "base"
            fast_copytree(self.repo_root, base, ignored=set(IGNORED_DIRS))
            self.base = base
        except Exception as exc:
            self.error = f"gate stage: {exc}"

    def stage(self, entries: dict[str, bytes]) -> Path | None:
        """A fresh work tree = pristine base clone + the proposal overlaid."""
        if self._thread is not None:
            self._thread.join(120)
        if self.base is None:
            return None
        try:
            self._n += 1
            work = self._root / f"work-{self._n}"
            fast_copytree(self.base, work)
            materialize_into(work, entries)
            return work
        except Exception:
            return None

    def close(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)

    # duck-typed alias so develop()'s failure paths can tear it down like the
    # remote GateWarmup they already handle
    def teardown(self) -> None:
        self.close()


def _format_guidance(kind: str, *, violations: list[str] | None = None, gate: GateResult | None = None) -> str:
    """Structured feedback injected into the worker's next attempt.

    Templates live in prompts.py (CRO-lite surface); {TOKENS} are substituted
    with str.replace — gate tails may contain braces, so never str.format.
    """
    from .prompts import get_prompt

    if kind == "policy":
        return get_prompt("guidance_policy").replace("{VIOLATIONS}", "\n- ".join(violations or []))
    if kind == "gate":
        assert gate is not None
        return (
            get_prompt("guidance_gate")
            .replace("{EXIT}", str(gate.exit_code))
            .replace("{TAIL}", gate.output_tail[-2000:])
        )
    raise ValueError(f"unknown guidance kind: {kind}")


_TIMEOUT_GUIDANCE = (
    "PREVIOUS ATTEMPT: you exceeded the wall-clock budget and were stopped "
    "mid-run. Be far more direct — make the minimal change and write it now; "
    "do not keep exploring or re-reading files."
)


def _prior_attempt_guidance(entries: dict[str, bytes], limit: int = 8000) -> str:
    """Render the worker's own last proposal so the next attempt iterates on it
    instead of restarting from scratch (#3c). Capped; empty when no entries."""
    if not entries:
        return ""
    parts = [
        f"--- {rel} ---\n{content.decode('utf-8', errors='replace')}"
        for rel, content in entries.items()
    ]
    body = "\n\n".join(parts)
    if len(body) > limit:
        body = body[:limit] + "\n[... truncated ...]"
    return (
        "YOUR PREVIOUS ATTEMPT proposed the files below. Build on this — fix the "
        "specific failure called out beneath; do NOT start from scratch:\n"
        f"{body}\n\n"
    )


# #A hard-kill at the source: reap the WHOLE worker process group on budget
# expiry. The base provider launches `perl -e 'alarm B; exec node runner …'` —
# SIGALRM kills only the runner, orphaning claude + MCP subprocesses. This perl
# forks the runner into its own session (setsid) and, on the alarm, kills the
# group (kill -PGID). It degrades safely at every step: fork failure → plain
# exec (framework-equivalent), setsid blocked → eval-guarded, killpg blocked →
# single-pid kill. So it can only match or improve the framework, never break
# the launch (which already relies on perl+exec).
_KILLTREE_PERL = (
    "my $b = shift @ARGV; my $pid = fork();"
    ' if (!defined $pid) { exec @ARGV or die "exec: $!" }'
    ' if ($pid == 0) { eval { require POSIX; POSIX::setsid() }; exec @ARGV or die "exec: $!" }'
    " $SIG{ALRM} = sub { kill('KILL', -$pid) or kill('KILL', $pid); exit 124 };"
    " alarm $b; waitpid($pid, 0); exit($? >> 8)"
)


def _swap_perl_killtree(argv: list) -> list:
    """Swap the base provider's perl `alarm; exec` script (argv[2]) for the
    killtree one, preserving everything else. A no-op if argv is not `perl -e …`."""
    if len(argv) >= 3 and str(argv[0]).endswith("perl") and argv[1] == "-e":
        argv = list(argv)
        argv[2] = _KILLTREE_PERL
    return argv


# Killtree + pump (verbose mode): same process-group hard stop as
# _KILLTREE_PERL, but the parent also pumps the child's stdout line by line to
# BOTH its own stdout (the substrate keeps parsing an identical stream) and a
# tee file (the live tailer's source), autoflushed so the tail is live. Takes
# TWO leading argv values: budget seconds, then the tee path.
#
# The alarm is armed FIRST, before pipe()/fork(): the timer survives exec, so
# the degenerate pipe/fork-failure fallbacks (plain `exec @ARGV`) still die at
# the budget — the framework's own baseline alarm+exec semantics. In those
# fallbacks the group kill and the watchdog's `exec @ARGV` cmdline marker are
# lost (the exec replaces the process image); only the budget stop remains.
# Keeps the literal `exec @ARGV` marker the watchdog greps for on the normal
# path.
_TEEPUMP_PERL = (
    "my $b = shift @ARGV; my $tee = shift @ARGV;"
    " alarm $b;"
    " my ($r, $w); pipe($r, $w) or do { exec @ARGV or die qq{exec: $!} };"
    " my $pid = fork();"
    ' if (!defined $pid) { exec @ARGV or die qq{exec: $!} }'
    " if ($pid == 0) { eval { require POSIX; POSIX::setsid() };"
    " close $r; open(STDOUT, q{>&}, $w) or die qq{dup: $!}; close $w;"
    ' exec @ARGV or die qq{exec: $!} }'
    " close $w; my $fh; open($fh, q{>}, $tee) or undef $fh;"
    " if ($fh) { my $old = select($fh); $| = 1; select($old) } $| = 1;"
    " $SIG{ALRM} = sub { kill(q{KILL}, -$pid) or kill(q{KILL}, $pid); exit 124 };"
    " while (my $line = <$r>) { print $line; print {$fh} $line if $fh }"
    " waitpid($pid, 0); exit($? >> 8)"
)


def _swap_perl_teepump(argv: list, tee_path) -> list:
    """Swap the perl script slot for the killtree+pump one and insert the tee
    path right after the budget seconds. A no-op if argv is not `perl -e …`."""
    if len(argv) >= 4 and str(argv[0]).endswith("perl") and argv[1] == "-e":
        argv = list(argv)
        argv[2] = _TEEPUMP_PERL
        argv.insert(4, str(tee_path))
    return argv


class _TailingExecution:
    """Proxy around the substrate ExecutionCapability (private seam, same
    degrade contract as set_worker_budget): starts the stream tailer just
    before the confined launch and drains it right after the launch returns —
    which is BEFORE the provider's finally-scrub deletes the scratch holding
    the tee file. A hook failure never blocks or fails the launch."""

    def __init__(self, inner, hook):
        self._inner = inner
        self._hook = hook

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def launch_confined(self, command, confinement):
        try:
            tailer = self._hook.start(self._inner.working_path)
        except Exception:
            tailer = None
        try:
            return self._inner.launch_confined(command, confinement)
        finally:
            if tailer is not None:
                try:
                    self._hook.drain(tailer)
                except Exception:
                    pass


def set_worker_budget(seconds: int, stream_hook=None) -> bool:
    """Raise the Claude workspace provider's wall-clock budget AND make it a hard
    kill of the whole worker process group (#A). With ``stream_hook`` (a
    ``events.WorkerStreamHook``), the launch perl additionally tees the worker's
    stream-json to a scratch file that the hook tails live (verbose mode).

    Alpha workaround for shepherd-ai 0.3.0: `budget`/`timeout` are reserved
    runtime fields and ClaudeHeadlessProvider hardcodes budget_seconds=240,
    too little for real features. Rebinds the internal transport seam
    (private API — revisit on framework upgrade), installing a provider whose
    launch perl reaps the process group at the budget instead of orphaning the
    worker's children. The watchdog (#B) is the belt-and-suspenders backstop.

    The whole rebind is a private-API seam that a framework upgrade may move or
    remove (#15). If any of it fails, we degrade gracefully — the worker runs on
    the framework's default budget (the #B watchdog still enforces the wall clock)
    — instead of crashing the run. Returns True when the rebind took effect.
    """
    try:
        from shepherd_dialect import providers
        from shepherd_dialect.workspace_control import runtime_provider as rp

        class _KillTreeProvider(providers.ClaudeHeadlessProvider):
            def command_argv(self, working_path, cli, prompt=None):
                # Reuse the framework's exact argv (all flags preserved) and swap ONLY
                # the perl script slot — robust to any change in the body, and a
                # no-op if the shape is ever not `perl -e`. With a stream hook the
                # swap is the killtree+pump variant (tee for the live tailer).
                argv = list(super().command_argv(working_path, cli, prompt))
                if stream_hook is not None:
                    try:
                        return _swap_perl_teepump(argv, stream_hook.tee_path(working_path))
                    except Exception:
                        pass  # verbose off, run intact
                return _swap_perl_killtree(argv)

            def execute(self, task_body, stack, context, args, *, execution=None, confinement=None):
                if stream_hook is not None and execution is not None:
                    execution = _TailingExecution(execution, stream_hook)
                return super().execute(
                    task_body, stack, context, args, execution=execution, confinement=confinement
                )

        def transport(invocation):
            kwargs = dict(
                provider_id=invocation.provider_id,
                prompt=invocation.prompt,
                model=invocation.model_name,
                budget_seconds=seconds,
            )
            try:
                return _KillTreeProvider(**kwargs)
            except Exception:
                return providers.ClaudeHeadlessProvider(**kwargs)  # never block the launch

        rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = rp._WorkspaceRuntimeProviderTransports(
            claude=transport
        )
        return True
    except Exception as exc:  # framework seam moved/removed on upgrade
        import sys as _sys

        print(f"warning: worker budget rebind unavailable ({type(exc).__name__}: {exc}); "
              "the watchdog still enforces the budget", file=_sys.stderr)
        return False


def build_diff_text(changeset, limit: int = DIFF_TEXT_LIMIT) -> str:
    """Render a retained changeset's content entries as reviewer-readable text."""
    parts: list[str] = []
    for rel, content in read_changeset_entries(changeset).items():
        text = content.decode("utf-8", errors="replace")
        parts.append(f"=== FILE: {rel} (proposed content) ===\n{text}")
    diff = "\n\n".join(parts)
    if len(diff) > limit:
        diff = diff[:limit] + f"\n\n[... truncated at {limit} chars ...]"
    return diff


def run_review(
    workspace,
    review_task,
    *,
    feature: str,
    changeset=None,
    diff_text: str | None = None,
    provider: str = "claude",
    placement: str = "jail",
    context_pack: str | None = None,
) -> ReviewVerdict:
    """Run the reviewer against a passing proposal.

    v0.2 lane limits (bindings need disjoint roots; multi-binding runs take no
    execution provider) rule out a syscall-read-only reviewer, so isolation is
    custody-based instead: the reviewer runs in the single-repo lane, its
    output is retained (never applied), a deterministic guard requires the
    changeset to be exactly {REVIEW.json}, and the output is always discarded
    after the verdict is read.
    """
    if diff_text is None:
        diff_text = build_diff_text(changeset)
    workspace.tasks.register(review_task)
    try:
        run = workspace.run(
            review_task,
            repo=workspace.git_repo(),
            placement=placement,
            runtime={"provider": provider},
            args={"feature": feature, "diff": diff_text, "context": context_pack or ""},
        )
    except Exception as exc:
        return ReviewVerdict(approved=False, summary="", error=f"review run failed: {exc}")

    output = run.output()
    try:
        entries = read_changeset_entries(output.changeset())
        touched = sorted(entries)
        if touched and touched != ["REVIEW.json"]:
            return ReviewVerdict(
                approved=False,
                summary="",
                error=f"reviewer touched files beyond REVIEW.json: {touched} — verdict invalidated",
            )
        if "REVIEW.json" not in entries:
            return ReviewVerdict(approved=False, summary="", error="reviewer produced no REVIEW.json")
        data = json.loads(entries["REVIEW.json"].decode("utf-8", errors="replace"))
    except Exception as exc:
        return ReviewVerdict(approved=False, summary="", error=f"invalid REVIEW.json: {exc}")
    finally:
        try:
            output.discard()
        except Exception:
            pass

    return ReviewVerdict(
        approved=bool(data.get("approved", False)),
        summary=str(data.get("summary", "")),
        issues=[str(i) for i in data.get("issues", [])],
    )


_TEST_FILE_RE = re.compile(
    r"(_test\.(py|exs|go)$"                       # foo_test.py / _test.exs / _test.go
    r"|\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs)$"   # foo.test.ts / foo.spec.js
    r"|(^|/)test_[^/]+\.py$)"                      # test_foo.py (pytest/unittest idiom)
)


def _proposal_has_rust_test(entries: dict[str, bytes]) -> bool:
    """Rust tests live in-module (#[test]/#[cfg(test)] in the same .rs) or under
    tests/ — a filename check isn't enough, so inspect content."""
    for rel, content in entries.items():
        if rel.startswith("tests/") and rel.endswith(".rs"):
            return True
        if rel.endswith(".rs"):
            text = content.decode("utf-8", errors="replace")
            if "#[test]" in text or "#[cfg(test)]" in text:
                return True
    return False


def _proposal_has_elixir_test(entries: dict[str, bytes]) -> bool:
    """An ExUnit test file shipped by the proposal (`*_test.exs` using ExUnit)."""
    for rel, content in entries.items():
        if rel.endswith("_test.exs"):
            text = content.decode("utf-8", errors="replace")
            if "ExUnit" in text or "use ExUnit.Case" in text or "test " in text:
                return True
    return False


def _resolve_gate_cmd(test_cmd: str, entries: dict[str, bytes]) -> str | None:
    """Resolve a native-gate command against the proposal's own tests.

    - {NEW_TESTS}: substitute the proposal's test files (node/python), or None
      if it shipped none.
    - {CARGO_TESTS}/{EXUNIT_TESTS}: presence sentinels for Rust/Elixir — their
      test runners pass with 0 tests, so require the proposal to contain a real
      test of that language, else None.
    Returns None when the proposal has no tests — the gate then fails loudly."""
    if "{CARGO_TESTS}" in test_cmd:
        if not _proposal_has_rust_test(entries):
            return None
        return test_cmd.replace("{CARGO_TESTS}", "").strip()
    if "{EXUNIT_TESTS}" in test_cmd:
        if not _proposal_has_elixir_test(entries):
            return None
        return test_cmd.replace("{EXUNIT_TESTS}", "").strip()
    if "{NEW_TESTS}" not in test_cmd:
        return test_cmd
    import shlex

    new_tests = sorted(rel for rel in entries if _TEST_FILE_RE.search(rel))
    if not new_tests:
        return None
    return test_cmd.replace("{NEW_TESTS}", " ".join(shlex.quote(t) for t in new_tests))


def _start_gate_warmup(repo_root: Path, test_cmd: str | None, timeout: int):
    """Speculative gate warmup (#2): pre-stage the gate environment in the
    background while the worker runs — the remote workdir when a remote gate
    is configured, else a pristine local repo copy (LocalGateStage) so the
    gate's per-attempt cost drops to a metadata clone + overlay. Returns a
    GateWarmup or LocalGateStage (the failure paths tear either down), or
    None when there is no gate."""
    if test_cmd is None:
        return None
    from . import config as _config

    cfg = _config.remote_gate(repo_root)
    if cfg is None:
        try:
            return LocalGateStage(repo_root).start()
        except Exception:
            return None
    from .remotegate import GateWarmup

    return GateWarmup(cfg, timeout=timeout).start()


def _run_gate(
    repo_root: Path,
    entries: dict[str, bytes],
    test_cmd: str,
    timeout: int,
    warmup=None,
    on_line=None,
    stage=None,
) -> GateResult:
    """Run the repo's test suite against a materialized copy of the proposal.

    If the repo configures a remote gate (test_remote), run it on the remote
    host instead of locally — for stacks whose build/test needs an environment
    the local sandbox lacks (a DB, a container, another architecture). A warmup,
    when given, is consumed by the remote gate (or torn down for a local gate).
    ``on_line`` (verbose mode) receives each merged output line of the test
    command as it happens — local and remote alike."""
    from . import config as _config

    if isinstance(warmup, LocalGateStage):  # accepted in either slot
        stage, warmup = warmup, None

    remote_cfg = _config.remote_gate(repo_root)
    if remote_cfg is not None:
        from .remotegate import run_remote_gate

        # Resolve native placeholders in the REMOTE test_cmd too (#11): a no-op for
        # ordinary user configs (no placeholder), but if a remote test_cmd uses
        # {NEW_TESTS}/{CARGO_TESTS}/{EXUNIT_TESTS} it now resolves against the
        # proposal's own tests instead of being sent raw.
        resolved_remote = _resolve_gate_cmd(remote_cfg.test_cmd, entries)
        if resolved_remote is None:
            if warmup is not None:
                warmup.teardown()
            return GateResult(
                False, 1,
                "native gate: the proposal contains no tests — write tests for the feature.",
            )
        if resolved_remote != remote_cfg.test_cmd:
            import dataclasses

            remote_cfg = dataclasses.replace(remote_cfg, test_cmd=resolved_remote)
        return run_remote_gate(remote_cfg, entries, timeout, warmup=warmup, on_line=on_line)
    if warmup is not None:
        warmup.teardown()  # a local gate can't use a remote warmup

    resolved = _resolve_gate_cmd(test_cmd, entries)
    if resolved is None:
        return GateResult(
            False, 1,
            "native gate: the proposal contains no tests (*.test.* / *_test.* / a Rust "
            "#[test]) — write tests for the feature; the gate needs them to pass.",
        )
    test_cmd = resolved

    def _judge(workdir: Path) -> GateResult:
        try:
            from .procstream import run_streaming

            res = run_streaming(
                test_cmd, shell=True, cwd=workdir, timeout=timeout, on_line=on_line
            )
        except OSError as exc:
            return GateResult(False, None, "", infra_error=f"could not run test suite: {exc}")
        if res.timed_out:
            return GateResult(False, None, "", infra_error=f"test suite timed out after {timeout}s")
        tail = res.output[-4000:]
        return GateResult(passed=res.returncode == 0, exit_code=res.returncode, output_tail=tail)

    if stage is not None:  # pre-staged base: per-attempt cost is a metadata clone
        try:
            work = stage.stage(entries)
            if work is not None:
                return _judge(work)
            # stage failed — fall through to the ordinary full materialize
        finally:
            stage.close()

    with tempfile.TemporaryDirectory(prefix="shepherd-gate-") as tmp:
        staged = Path(tmp) / "staged"
        try:
            _materialize(repo_root, entries, staged)
        except Exception as exc:
            return GateResult(False, None, "", infra_error=f"materialize failed: {exc}")
        return _judge(staged)


def develop(
    workspace,
    task,
    *,
    repo,
    repo_root: Path,
    feature: str,
    test_cmd: str | None,
    provider: str = "claude",
    placement: str = "jail",
    max_attempts: int = 3,
    gate_timeout: int = 600,
    policy: ChangesetPolicy | None = None,
    extra_args: dict | None = None,
    review_task=None,
    initial_guidance: str = "",
    context_pack: str | None = None,
    reporter=None,
    worker_budget: int | None = None,
    event_log=None,
    stream_hook=None,
    speculative_review: bool = False,
) -> DevReport:
    """Supervised development loop. Returns a report; never mutates the workspace.

    test_cmd=None skips the test gate (policy-only pass) — used by the parallel
    coordinator, whose combined gate judges the merged proposal instead.
    initial_guidance seeds the first attempt (e.g. teammate/handoff context);
    later attempts replace it with concrete failure feedback. context_pack, when
    given, is prepended to the guidance of EVERY attempt (built once per command,
    reused across retries — the lane-honest analogue of prefix reuse). reporter
    surfaces live per-phase progress (#A); defaults to a silent NullProgress.
    event_log (verbose mode) receives normalized run events — phases, per-file
    diffs, streamed gate lines/failures, policy and review outcomes; stream_hook
    is the WorkerStreamHook whose attempt counter develop keeps current.
    """
    import time as _time

    from .progress import NullProgress, worker_activity_summary

    reporter = reporter or NullProgress()
    policy = policy or ChangesetPolicy()
    report = DevReport(feature=feature, succeeded=False, repo=str(repo_root))
    guidance = initial_guidance

    workspace.tasks.register(task)

    def _emit(kind: str, payload: dict | None = None, attempt: int | None = None):
        if event_log is not None:
            try:
                event_log.emit(kind, payload, attempt=attempt)
            except Exception:
                pass

    for number in range(1, max_attempts + 1):
        warmup = _start_gate_warmup(repo_root, test_cmd, gate_timeout)
        reporter.step(f"attempt {number}/{max_attempts} · worker running")
        _emit("phase.start", {"label": "worker", "max_attempts": max_attempts}, attempt=number)
        if stream_hook is not None:
            try:
                # set_attempt writes whichever slot the hook actually reads for
                # this thread (thread-local when bound — parallel candidates).
                stream_hook.set_attempt(number)
            except Exception:
                pass
        args = {"repo": repo, **(extra_args or {})}
        if "output_path" not in args:  # real worker takes feature/guidance
            args["feature"] = feature
            args["guidance"] = (
                f"{context_pack}\n\n{guidance}".strip() if context_pack else guidance
            )

        # #B backstop: hard-kill the worker subtree `grace` past the budget (serial
        # claude runs only — parallel best-of can't tell workers apart by signature).
        wd = None
        if worker_budget and provider == "claude" and test_cmd is not None:
            from .worker_watchdog import WorkerWatchdog

            wd = WorkerWatchdog(worker_budget).start()
        started = _time.monotonic()
        try:
            run = workspace.run(
                task,
                placement=placement,
                runtime={"provider": provider},
                **args,
            )
        except Exception as exc:
            if wd is not None:
                wd.cancel()
            if warmup is not None:
                warmup.teardown()
            if wd is not None and wd.fired:
                reporter.fail("worker timed out (budget hard-kill)")
                report.attempts.append(Attempt(
                    number, "(timed out)", [], [], None, "timed_out",
                    error="worker exceeded budget and was hard-killed",
                    duration_s=round(_time.monotonic() - started, 1)))
                guidance = _TIMEOUT_GUIDANCE
                continue
            reporter.fail(f"worker run failed: {type(exc).__name__}")
            report.attempts.append(
                Attempt(
                    number, "(no run)", [], [], None, "run_failed",
                    error=f"{type(exc).__name__}: {exc}",
                    duration_s=round(_time.monotonic() - started, 1),
                )
            )
            guidance = (
                "PREVIOUS ATTEMPT: the agent run itself failed "
                f"({type(exc).__name__}). Work efficiently and stay within the "
                "wall-clock budget: read only what you need, then write the change."
            )
            continue
        if wd is not None:
            wd.cancel()
            if wd.fired:  # killed but workspace.run returned — its output is garbage
                try:
                    run.output().discard()
                except Exception:
                    pass
                if warmup is not None:
                    warmup.teardown()
                reporter.fail("worker timed out (budget hard-kill)")
                report.attempts.append(Attempt(
                    number, getattr(run, "run_ref", "(timed out)"), [], [], None, "timed_out",
                    error="worker exceeded budget and was hard-killed",
                    duration_s=round(_time.monotonic() - started, 1)))
                guidance = _TIMEOUT_GUIDANCE
                continue
        duration = round(_time.monotonic() - started, 1)
        output = run.output()
        changeset = output.changeset()
        entries = read_changeset_entries(changeset)
        changed = list(entries)
        reporter.note(worker_activity_summary(run, entries))  # post-hoc #B
        _emit("attempt.diff", {"files": changed, "run_ref": run.run_ref}, attempt=number)

        if not changed:
            # Worker produced nothing: either it judged the feature already
            # satisfied in its world basis, or the agent run failed silently.
            output.discard()
            if warmup is not None:
                warmup.teardown()
            reporter.fail("no file changes")
            _emit("phase.fail", {"label": "worker", "reason": "no file changes"}, attempt=number)
            report.attempts.append(Attempt(number, run.run_ref, changed, [], None, "no_change", duration_s=duration))
            guidance = (
                "PREVIOUS ATTEMPT: you produced no file changes at all. "
                "If the feature genuinely already exists, say so by making the "
                "minimal change that proves it (e.g. a test); otherwise implement it now."
            )
            continue

        verdict_policy = check_paths(changed, policy)
        if not verdict_policy.ok:
            output.discard()
            if warmup is not None:
                warmup.teardown()
            reporter.fail(f"policy: {len(verdict_policy.violations)} violation(s)")
            _emit("policy.reject", {"violations": verdict_policy.violations}, attempt=number)
            report.attempts.append(
                Attempt(number, run.run_ref, changed, verdict_policy.violations, None, "policy_rejected", duration_s=duration)
            )
            guidance = _prior_attempt_guidance(entries) + _format_guidance("policy", violations=verdict_policy.violations)
            continue

        gate: GateResult | None = None
        # Speculative review (opt-in): the reviewer needs only the proposal —
        # not the gate's verdict — so overlap the two and hide min(gate,
        # review) from the wall clock. On gate failure the verdict is thrown
        # away (tokens spent on a proposal that died; hence opt-in).
        spec_result: dict = {}
        spec_thread = None
        if test_cmd is not None and review_task is not None and speculative_review:
            def _speculate():
                try:
                    spec_result["verdict"] = run_review(
                        workspace,
                        review_task,
                        feature=feature,
                        changeset=changeset,
                        provider=provider,
                        placement=placement,
                        context_pack=context_pack,
                    )
                except Exception:
                    pass  # spec failure → the sequential path below reruns it

            spec_thread = threading.Thread(
                target=_speculate, daemon=True, name="shepherd-spec-review"
            )
            spec_thread.start()
        if test_cmd is not None:
            reporter.step(f"attempt {number} · gate")
            _emit("phase.start", {"label": "gate"}, attempt=number)
            on_line = None
            if event_log is not None:
                from .events import gate_line_observer

                on_line = gate_line_observer(event_log, attempt=number)
            gate = _run_gate(repo_root, entries, test_cmd, gate_timeout, warmup=warmup, on_line=on_line)
            _emit(
                "gate.result",
                {"passed": gate.passed, "exit_code": gate.exit_code, "infra_error": gate.infra_error},
                attempt=number,
            )
            if gate.infra_error:
                # Suite could not run: abort, do not burn attempts, keep output retained.
                # Surface the run-ref so the summary tells the user how to settle/reject
                # the retained proposal — else the next run blocks on a pending output
                # with no visible ref (#9).
                reporter.fail(f"gate infra: {gate.infra_error[:80]}")
                report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "tests_failed", duration_s=duration))
                report.final_run_ref = run.run_ref
                report.entries = entries
                report.settlement_hint = f"gate infra error: {gate.infra_error}"
                return report

            if not gate.passed:
                output.discard()
                reporter.fail(f"gate failed (exit {gate.exit_code})")
                report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "tests_failed", duration_s=duration))
                guidance = _prior_attempt_guidance(entries) + _format_guidance("gate", gate=gate)
                continue

        report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "passed", duration_s=duration))
        report.succeeded = True
        report.final_run_ref = run.run_ref
        report.entries = entries
        if review_task is not None:
            reporter.step(f"attempt {number} · review")
            _emit("phase.start", {"label": "review"}, attempt=number)
            if spec_thread is not None:
                spec_thread.join()  # already ran overlapped with the gate
                report.review = spec_result.get("verdict")
            if report.review is None:
                report.review = run_review(
                    workspace,
                    review_task,
                    feature=feature,
                    changeset=changeset,
                    provider=provider,
                    placement=placement,
                    context_pack=context_pack,
                )
            if report.review is not None:
                _emit("review.verdict", {"approved": report.review.approved}, attempt=number)
                for issue in report.review.issues or []:
                    _emit("review.issue", {"text": str(issue)}, attempt=number)
        return report

    return report
