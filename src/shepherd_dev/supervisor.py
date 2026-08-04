"""Supervisor meta-agent: runs a worker task in a sandbox, applies the
changeset policy, gates the retained output on the repo's test suite, and
retries with injected guidance on failure.

The supervisor NEVER applies anything to the workspace. A passing attempt
stays retained; settlement (select/apply/discard) is always a human decision.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .diffcollect import Entries
from .ledger import Ledger
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
    #: ids of previously-open findings this review checked and found gone —
    #: the only thing that closes one short of approval (see ledger.py)
    resolved: list[str] = field(default_factory=list)
    error: str | None = None  # review ran but verdict could not be obtained
    #: True when no model read the diff — the verdict is a deterministic
    #: heuristic over its SHAPE (file count, byte size). Useful as a signal,
    #: never as authority: --auto-settle refuses it, because approving a change
    #: nobody read is the machine settling its own work.
    advisory: bool = False


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
    # set when the cycle stopped for a reason no further iteration can fix
    # (no progress, nothing actionable left) — outranks every other state
    blocked_reason: str | None = None
    # every issue the reviewer raised, and how each one ended (see ledger.py)
    ledger: Ledger | None = None

    @property
    def outcome(self) -> str:
        """What actually happened, in one word an orchestrator can route on.

        `succeeded` only ever meant "the gate passed". Saying `passed_approved`
        for a --no-review run, or letting `succeeded: true` stand alone next to
        a REJECTED verdict, is the false claim this whole surface exists to
        kill — so absence of a verdict reads as `passed_unreviewed`, never as
        approval. Mirrors `_auto_settle_conditions`; the two must not drift.
        """
        if self.blocked_reason:
            return "blocked"
        if not self.succeeded:
            return "failed"
        if self.review is None or self.review.error:
            return "passed_unreviewed"
        return "passed_approved" if self.review.approved else "passed_rejected"

    def summary(self) -> str:
        lines = [
            f"feature: {self.feature}",
            f"succeeded: {self.succeeded}",
            f"outcome: {self.outcome}",
        ]
        if self.blocked_reason:
            lines.append(f"blocked: {self.blocked_reason}")
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
        if self.ledger is not None:
            rendered = self.ledger.render()  # unprompted: nobody has to ask
            if rendered:
                lines.append(rendered)
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


def materialize_into(
    root: Path, entries: dict[str, bytes], progress: list[str] | None = None
) -> list[str]:
    """Write changeset content entries under root.

    Refuses paths that escape root. Returns the list of written paths. When
    `progress` is given it receives each path as it lands, so a caller that has
    to report a PARTIAL write still knows what reached the disk (the return
    value is lost when this raises).

    Paths in the entries' `.executable` set (an Entries attribute; see
    diffcollect) are chmod'd 0o755 after the write — the exec bit a worker set
    with chmod +x must survive onto the gate tree and, after settle, onto the
    real repo. A plain dict carries no such set and gets the filesystem
    default, exactly the behavior before modes were tracked.
    """
    written: list[str] = progress if progress is not None else []
    executable = getattr(entries, "executable", frozenset())
    resolved_root = root.resolve()
    for rel, content in entries.items():
        target = (root / rel).resolve()
        if not target.is_relative_to(resolved_root):
            raise ValueError(f"changeset path escapes repo root: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if rel in executable:
            target.chmod(0o755)
        written.append(rel)
    return written


def read_changeset_entries(changeset) -> Entries:
    """Snapshot a retained changeset's content entries into memory.

    v0.3.0 lane reality (verified on 3 workspaces): runs fork from the
    workspace's ORIGINAL adoption basis, not from later settlements, so
    changed_paths can list stale-basis artifacts whose content is
    unavailable (read_file -> None). Those are NOT worker actions; the
    git worktree is our source of truth, so they are skipped. Consequence:
    genuine worker deletions cannot be expressed in this lane (documented
    limitation; effect-stream support in F3).

    read_file yields (bytes, git filemode); the mode's exec bits (0o100755
    for a blob the worker chmod'd +x, or any plain mode with an x bit) are
    kept in the result's `.executable` set so the writers downstream can
    re-apply them — discarding them here is how +x proposals used to land
    as 0o644.
    """
    entries: Entries = Entries()
    executable: set[str] = set()
    for rel in changeset.changed_paths:
        entry = changeset.read_file(rel)  # (bytes, mode) | None
        if entry is not None:
            content, mode = entry
            entries[rel] = content
            if mode & 0o111:
                executable.add(rel)
    entries.executable = frozenset(executable)
    return entries


#: Environment the gate must NOT inherit (#4). The gate exists to judge the
#: PROPOSAL's tree; these point the child's interpreter and test harness back at
#: the supervisor's own checkout — PYTHONPATH puts our source on the suite's
#: sys.path, VIRTUAL_ENV/PYTHONHOME redirect the interpreter, and the PYTEST_*
#: pair leaks the state of whatever pytest run launched us into the child's.
#: Deliberately narrow: a blanket scrub would take PATH/HOME and the project's
#: own configuration with it, and break every real gate.
GATE_ENV_STRIP = frozenset({
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "VIRTUAL_ENV",
    "PYTEST_CURRENT_TEST",
    "PYTEST_ADDOPTS",
    "__PYVENV_LAUNCHER__",
})

#: Environment no child interpreter of OURS may inherit.
#:
#: PYTHONHOME names a stdlib prefix; a wrong one stops the interpreter booting
#: at all, before any of our code runs. Every place shepherd spawns its own
#: interpreter — the staged-tree remover, the CRO replay, `shepherd init` in a
#: clone — therefore fails for a reason that has nothing to do with the work,
#: and _remove_tree's `check=False` made that failure invisible entirely.
#: __PYVENV_LAUNCHER__ can redirect which interpreter runs, with the same effect.
#:
#: Deliberately NOT PYTHONPATH. That one is load-bearing: `-m shepherd_dev.cli`
#: needs the package importable, and anyone running from a source checkout gets
#: that from PYTHONPATH. Stripping it would trade a rare breakage for a common
#: one. Only children that import nothing but the stdlib (the tree remover) can
#: afford the wider gate_env scrub.
CHILD_PYTHON_ENV_STRIP = frozenset({"PYTHONHOME", "__PYVENV_LAUNCHER__"})


def child_python_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for spawning one of our own interpreters."""
    src = dict(os.environ if base is None else base)
    return {k: v for k, v in src.items() if k not in CHILD_PYTHON_ENV_STRIP}


#: Comma-separated overrides for the list above, for a gate that genuinely needs
#: an inherited PYTHONPATH (KEEP) or a project whose own variables must not
#: cross into the sandbox (STRIP).
_GATE_ENV_KEEP_VAR = "SHEPHERD_DEV_GATE_ENV_KEEP"
_GATE_ENV_STRIP_VAR = "SHEPHERD_DEV_GATE_ENV_STRIP"


def _env_list(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


def gate_env(
    base: dict[str, str] | None = None,
    *,
    keep: list[str] | None = None,
    strip: list[str] | None = None,
) -> dict[str, str]:
    """The environment a gate subprocess runs under: `base` (default: ours)
    minus GATE_ENV_STRIP. `keep` re-admits entries, `strip` removes more; both
    default to the SHEPHERD_DEV_GATE_ENV_{KEEP,STRIP} env lists."""
    src = dict(os.environ if base is None else base)
    kept = set(keep if keep is not None else _env_list(_GATE_ENV_KEEP_VAR))
    dropped = set(GATE_ENV_STRIP | set(strip if strip is not None else _env_list(_GATE_ENV_STRIP_VAR))) - kept
    return {k: v for k, v in src.items() if k not in dropped}


def _prune_ignored(root: Path, ignored: set[str]) -> None:
    """Remove entries named in ``ignored`` at any depth under an
    already-copied tree. `cp -R`/shutil.copytree copy a top-level entry's
    subtree wholesale once started, blind to names below that entry — a
    nested `.git` (an Elixir `mix.exs` git dependency under deps/<pkg>/.git
    is the common case) rides along uncaught otherwise."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        keep = []
        for name in dirnames:
            if name in ignored:
                shutil.rmtree(Path(dirpath) / name, ignore_errors=True)
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            if name in ignored:
                (Path(dirpath) / name).unlink(missing_ok=True)


def fast_copytree(src: Path, dest: Path, ignored: set[str] | None = None) -> None:
    """Tree copy tuned for the gate/clone hot path: per top-level entry, try
    the filesystem's cheap copy (`cp -c` clonefile on APFS, `cp -R` elsewhere
    — measured 3.5× faster than shutil.copytree on a 1500-file tree) and fall
    back to shutil.copytree per entry. ``ignored`` names are skipped at the
    top level (where .git/.venv/node_modules live) AND pruned afterward at
    every nested level, since a copied subtree can itself contain them."""
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
        if ignored and target.is_dir() and not target.is_symlink():
            _prune_ignored(target, ignored)


#: Dependency dirs the gate's staged copy NEEDS but must not duplicate: they
#: are excluded from every tree copy (state hygiene + size) and symlinked back
#: from the real repo instead — `npm test` without node_modules, or a
#: `.venv/bin/pytest` gate, would otherwise fail for the wrong reason.
DEP_DIRS = (".venv", "node_modules")


def _link_dep_dirs(repo_root: Path, dest: Path) -> None:
    """Symlink the repo's dependency dirs into a staged copy. Best-effort."""
    for name in DEP_DIRS:
        src = Path(repo_root) / name
        target = dest / name
        try:
            if src.is_dir() and not target.exists() and not target.is_symlink():
                target.symlink_to(src.resolve())
        except Exception:
            pass  # a missing dep link degrades to the pre-fix behavior


def _remove_tree(path: Path) -> None:
    """Delete a staged tree from OUTSIDE this process.

    A staged copy holds symlinks back to the repo's dependency dirs
    (_link_dep_dirs). shutil.rmtree deletes entries by bare name against a
    directory fd, and the substrate's os.unlink patch resolves that candidate
    with Path.resolve() — which follows the symlink onto the real .venv inside
    the OPEN workspace and refuses it as an unscoped mutation. That refusal is
    not an OSError, so ignore_errors=True does not swallow it: teardown then
    takes the whole run down with it, mid-gate.

    A child interpreter carries none of the patches, so the same rmtree there
    is just a delete. Verified: with the workspace open, an in-process rmtree
    over a stage holding a link into the workspace raises; over a stage holding
    only real files, or a link pointing outside, it does not.
    """
    path = Path(path)
    if not path.exists():
        return
    try:
        subprocess.run(
            [sys.executable, "-c",
             "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)",
             str(path)],
            check=False, timeout=120,
            # Scrubbed, because this child is OUR interpreter: a PYTHONHOME or
            # PYTHONPATH pointing somewhere else stops it booting at all, and
            # with check=False the failure is invisible — every staged tree then
            # leaks in silence. gate_env drops exactly those.
            env=gate_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # a leaked temp dir is the OS's problem; killing the run is not


def _materialize(repo_root: Path, entries: dict[str, bytes], dest: Path) -> None:
    """Copy the repo and overlay the proposal's content entries on top."""
    fast_copytree(Path(repo_root), dest, ignored=set(IGNORED_DIRS))
    _link_dep_dirs(repo_root, dest)
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
        self._lock = threading.Lock()

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
        """A fresh work tree = pristine base clone + the proposal overlaid.

        Safe to call repeatedly and from several threads: one base serves every
        gate that borrows this stage (run2's combined gate and its repairs,
        best-of's K candidates), and the counter that names each work tree is
        taken under a lock so two callers can never land on the same path.
        """
        if self._thread is not None:
            self._thread.join(120)
        if self.base is None:
            return None
        try:
            with self._lock:
                self._n += 1
                work = self._root / f"work-{self._n}"
            fast_copytree(self.base, work)
            _link_dep_dirs(self.repo_root, work)
            materialize_into(work, entries)
            return work
        except Exception:
            return None

    def close(self) -> None:
        _remove_tree(self._root)

    # duck-typed alias so develop()'s failure paths can tear it down like the
    # remote GateWarmup they already handle
    def teardown(self) -> None:
        self.close()


def _format_guidance(
    kind: str,
    *,
    violations: list[str] | None = None,
    gate: GateResult | None = None,
    ledger: Ledger | None = None,
) -> str:
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
    if kind == "review":
        assert ledger is not None
        return get_prompt("guidance_review").replace("{FINDINGS}", ledger.guidance())
    raise ValueError(f"unknown guidance kind: {kind}")


def _attempt_label(
    attempts_left: int, max_attempts: int, round_no: int, review_rounds: int
) -> str:
    """Where this attempt sits in the budget it is actually spending.

    Two counters run at once: attempts are numbered across the whole run and
    never reset, while the allowance they draw on resets with every rework
    round. Printing the run-wide number against `max_attempts` therefore
    describes the wrong quantity — a second round's first attempt reads as
    `attempt 2/2`, apparently out of budget while in fact holding a full fresh
    one, and a third would read `attempt 3/2`.

    So the numerator is the position within the round, and the round is named
    whenever there has been more than one. A single-round run — every run that
    is not reworked — prints exactly what it printed before.
    """
    position = max_attempts - attempts_left
    if round_no <= 1:
        return f"attempt {position}/{max_attempts}"
    return f"attempt {position}/{max_attempts} · round {round_no}/{review_rounds}"


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
#
# The handler is installed BEFORE anything can arm the timer (#11): while
# SIGALRM carries its default disposition the process dies bare — no group
# kill, no exit 124. $pid is declared first so the closure can capture it and
# check definedness, since the alarm may in principle fire before fork returns.
_KILLTREE_PERL = (
    "my $b = shift @ARGV; my $pid;"
    " $SIG{ALRM} = sub { if (defined $pid) { kill('KILL', -$pid) or kill('KILL', $pid) } exit 124 };"
    " $pid = fork();"
    ' if (!defined $pid) { exec @ARGV or die "exec: $!" }'
    ' if ($pid == 0) { eval { require POSIX; POSIX::setsid() }; exec @ARGV or die "exec: $!" }'
    " alarm $b; waitpid($pid, 0); exit($? >> 8)"
)


def _swap_perl_killtree(argv: list) -> list:
    """Swap the base provider's perl `alarm; exec` script (argv[2]) for the
    killtree one, preserving everything else. A no-op if argv is not `perl -e …`."""
    if len(argv) >= 3 and str(argv[0]).endswith("perl") and argv[1] == "-e":
        argv = list(argv)
        argv[2] = _KILLTREE_PERL
    return argv


_SANDBOX_TMPDIR_VAR = "CLAUDE_CODE_TMPDIR"


def _with_sandbox_tmpdir(argv: list) -> list:
    """Keep the jailed worker's own Bash sandbox scratch inside the jail.

    The worker runs under a syscall jail whose writable roots are the run's
    clone. The Claude CLI puts its Bash-sandbox scratch under a tmp root that
    on darwin is a hardcoded `/tmp` — it does NOT read TMPDIR there — so the
    first Bash call tries to mkdir `/tmp/claude-<uid>/<flattened-cwd>`, outside
    the writable roots. The jail denies it and every Bash call fails with
    `EPERM: operation not permitted, mkdir …`, which silently degrades both
    agents to file tools: the worker cannot run the suite it just changed, and
    the reviewer reviews by inspection with nothing executed.

    CLAUDE_CODE_TMPDIR overrides that root. It is pointed at the value the
    provider already chose for TMPDIR — the per-run scratch it creates before
    launch and scrubs after — so the sandbox lands inside the jail and the
    credential-free scratch stays the only thing that leaks out (nothing).

    Reads the value off the argv rather than recomputing it, so a framework
    that moves its scratch moves this with it. A no-op if the env prefix ever
    stops carrying TMPDIR, or if the variable is already set.
    """
    out = list(argv)
    if any(str(item).startswith(f"{_SANDBOX_TMPDIR_VAR}=") for item in out):
        return out
    for index, item in enumerate(out):
        text = str(item)
        if text.startswith("TMPDIR="):
            out.insert(index + 1, f"{_SANDBOX_TMPDIR_VAR}={text[len('TMPDIR='):]}")
            return out
    return out  # env prefix shape changed: leave the launch alone


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
#
# The HANDLER therefore has to be installed even earlier (#11). Arming before
# pipe/fork is load-bearing, so it cannot move; but installing the handler only
# after the fork left a window — pipe() plus fork() — in which SIGALRM still
# carried its default disposition, killing the pump bare with no group kill and
# no exit 124. $pid is declared up front so the closure captures it and can
# check definedness for a fire that lands inside that window.
_TEEPUMP_PERL = (
    "my $b = shift @ARGV; my $tee = shift @ARGV; my $pid;"
    " $SIG{ALRM} = sub { if (defined $pid) { kill(q{KILL}, -$pid) or kill(q{KILL}, $pid) } exit 124 };"
    " alarm $b;"
    " my ($r, $w); pipe($r, $w) or do { exec @ARGV or die qq{exec: $!} };"
    " $pid = fork();"
    ' if (!defined $pid) { exec @ARGV or die qq{exec: $!} }'
    " if ($pid == 0) { eval { require POSIX; POSIX::setsid() };"
    " close $r; open(STDOUT, q{>&}, $w) or die qq{dup: $!}; close $w;"
    ' exec @ARGV or die qq{exec: $!} }'
    " close $w; my $fh; open($fh, q{>}, $tee) or undef $fh;"
    " if ($fh) { my $old = select($fh); $| = 1; select($old) } $| = 1;"
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

    Raises ValueError for a non-positive budget: perl's `alarm 0` CANCELS the
    timer instead of firing it, so a budget of 0 silently removed the hard kill
    rather than applying it, and the #B watchdog keys off the same number — the
    run would have been left with nothing enforcing its wall clock (#11).
    """
    if seconds <= 0:
        raise ValueError(
            f"worker budget must be positive (got {seconds}); "
            "a budget of 0 disables the hard kill instead of enforcing it"
        )
    try:
        from shepherd_dialect import providers
        from shepherd_dialect.workspace_control import runtime_provider as rp

        class _KillTreeProvider(providers.ClaudeHeadlessProvider):
            def command_argv(self, working_path, cli, prompt=None):
                # Reuse the framework's exact argv (all flags preserved) and swap ONLY
                # the perl script slot — robust to any change in the body, and a
                # no-op if the shape is ever not `perl -e`. With a stream hook the
                # swap is the killtree+pump variant (tee for the live tailer).
                argv = _with_sandbox_tmpdir(super().command_argv(working_path, cli, prompt))
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
    findings: str = "",
) -> ReviewVerdict:
    """Run the reviewer against a passing proposal.

    `findings` carries the still-open findings of earlier rounds, so this
    reviewer answers about them by id instead of re-deriving the same
    objections in fresh words — which is what let a re-raised finding read as
    one item closed and one item newly found (see ledger.py).

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
            args={
                "feature": feature,
                "diff": diff_text,
                "context": context_pack or "",
                "findings": findings,
            },
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
        # `is True`, not bool(). bool() fails OPEN on the answer a model most
        # plausibly gets wrong — bool("false") is True — and an approval feeds
        # --auto-settle, which writes without asking. Only the JSON literal
        # `true` is the reviewer saying yes.
        approved=data.get("approved") is True,
        summary=str(data.get("summary", "")),
        issues=[str(i) for i in data.get("issues", [])],
        resolved=[str(i) for i in (data.get("resolved") or [])],
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


def start_local_gate_stage(repo_root: Path, test_cmd: str | None):
    """A LocalGateStage for callers that run MANY gates off one base (A3):
    run2's combined gate plus its repairs, best-of's K candidates.

    Local only, deliberately. A remote GateWarmup is single-use — run_remote_gate
    adopts its workdir and tears it down — so it cannot be shared across gates,
    and a remote-gated repo simply keeps the ordinary path. None when there is
    no gate, or when staging cannot start (the gate then falls back to the full
    materialize it did before).
    """
    if test_cmd is None:
        return None
    from . import config as _config

    if _config.remote_gate(repo_root) is not None:
        return None
    try:
        return LocalGateStage(repo_root).start()
    except Exception:
        return None


def _run_gate(
    repo_root: Path,
    entries: dict[str, bytes],
    test_cmd: str,
    timeout: int,
    warmup=None,
    on_line=None,
    stage=None,
    keep_stage: bool = False,
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
                test_cmd, shell=True, cwd=workdir, timeout=timeout, on_line=on_line,
                env=gate_env(),
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
            # keep_stage: the caller runs MANY gates off one base (run2's
            # combined gate plus repairs, best-of's K candidates) and closes it
            # itself. Closing here would throw the base away after the first.
            if not keep_stage:
                stage.close()

    # Not TemporaryDirectory: its cleanup is the same in-process rmtree that
    # _remove_tree exists to avoid, and this tree carries the same dep links.
    tmp = Path(tempfile.mkdtemp(prefix="shepherd-gate-"))
    try:
        staged = tmp / "staged"
        try:
            _materialize(repo_root, entries, staged)
        except Exception as exc:
            return GateResult(False, None, "", infra_error=f"materialize failed: {exc}")
        return _judge(staged)
    finally:
        _remove_tree(tmp)


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
    review_rounds: int = 1,
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
    gate_lock=None,
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
    review_rounds > 1 lets a REJECTED-but-passing proposal be reworked: the
    ledger's open findings become the next pass's guidance. Rework runs on its
    own budget — a gate failure never eats the rework allowance, and a rework
    never eats the attempts reserved for failures.
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

    ledger = Ledger() if review_task is not None else None
    report.ledger = ledger
    # Two budgets, deliberately not shared: attempts absorb failures (a broken
    # run, a policy violation, a red suite), rounds absorb objections. Letting
    # them draw on each other would mean a run that failed the gate twice can
    # no longer act on the reviewer, which is the case that most needs it.
    rounds_left = max(0, review_rounds - 1)
    round_no = 1
    attempts_left = max_attempts
    number = 0
    last_entries: dict[str, bytes] | None = None

    while attempts_left > 0:
        number += 1
        attempts_left -= 1
        warmup = _start_gate_warmup(repo_root, test_cmd, gate_timeout)
        reporter.step(f"{_attempt_label(attempts_left, max_attempts, round_no, review_rounds)} · worker running")
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

        if entries == last_entries:
            # The worker handed back byte-for-byte what was just rejected, so
            # the feedback did not land. Re-judging it would spend the rest of
            # the budget to reach the same verdict, and "attempts exhausted"
            # would read like a hard task instead of a stuck loop.
            output.discard()
            if warmup is not None:
                warmup.teardown()
            report.blocked_reason = (
                f"no progress: attempt {number} proposed the same files as attempt {number - 1}"
            )
            reporter.fail("no progress (identical changeset)")
            _emit("attempt.no_progress", {"attempt": number, "files": changed}, attempt=number)
            report.attempts.append(
                Attempt(number, run.run_ref, changed, [], None, "no_progress", duration_s=duration)
            )
            return report
        last_entries = entries

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

        def _reap_spec():
            """Reap the speculative reviewer and hand back its verdict (None if
            it never produced one). EVERY exit from this attempt must call it:
            the thread holds `workspace` and reads the very changeset the
            failure paths discard, so one left running both races
            output.discard() and overlaps the NEXT attempt's workspace.run.
            The join only ever sat on the review path, which both gate-failure
            exits jump straight over."""
            if spec_thread is not None:
                spec_thread.join()
            return spec_result.get("verdict")

        if test_cmd is not None:
            reporter.step(f"{_attempt_label(attempts_left, max_attempts, round_no, review_rounds)} · gate")
            _emit("phase.start", {"label": "gate"}, attempt=number)
            on_line = None
            if event_log is not None:
                from .events import gate_line_observer

                on_line = gate_line_observer(event_log, attempt=number)
            # gate_lock (runN): parallel lanes overlap their workers but take
            # turns on the CPU-heavy gate — never two suites at once.
            if gate_lock is not None:
                with gate_lock:
                    gate = _run_gate(
                        repo_root, entries, test_cmd, gate_timeout, warmup=warmup, on_line=on_line
                    )
            else:
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
                _reap_spec()
                reporter.fail(f"gate infra: {gate.infra_error[:80]}")
                report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "tests_failed", duration_s=duration))
                report.final_run_ref = run.run_ref
                report.entries = entries
                report.settlement_hint = f"gate infra error: {gate.infra_error}"
                return report

            if not gate.passed:
                _reap_spec()  # before discard(): the reviewer is still reading it
                output.discard()
                reporter.fail(f"gate failed (exit {gate.exit_code})")
                report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "tests_failed", duration_s=duration))
                guidance = _prior_attempt_guidance(entries) + _format_guidance("gate", gate=gate)
                continue

        report.attempts.append(Attempt(number, run.run_ref, changed, [], gate, "passed", duration_s=duration))
        report.succeeded = True
        report.final_run_ref = run.run_ref
        report.entries = entries
        if review_task is None:
            _reap_spec()
            return report

        reporter.step(f"{_attempt_label(attempts_left, max_attempts, round_no, review_rounds)} · review")
        _emit("phase.start", {"label": "review"}, attempt=number)
        # A local, not report.review: on a rework round the previous verdict is
        # still on the report, and reusing it would skip this round's review.
        verdict = _reap_spec()  # already ran overlapped with the gate
        if verdict is None:
            verdict = run_review(
                workspace,
                review_task,
                feature=feature,
                changeset=changeset,
                provider=provider,
                placement=placement,
                context_pack=context_pack,
                findings=ledger.guidance() if ledger is not None else "",
            )
        report.review = verdict
        if verdict is not None:
            _emit("review.verdict", {"approved": verdict.approved}, attempt=number)
            for issue in verdict.issues or []:
                _emit("review.issue", {"text": str(issue)}, attempt=number)
            if not verdict.error and ledger is not None:
                # Folded in even on approval: approval is the judgement that
                # closes whatever the reviewer left open. Short of it, only an
                # explicit `resolved` closes anything — a rejecting reviewer
                # that merely stopped mentioning an item has said nothing
                # about it, and usually just reworded it.
                ledger.record_round(
                    round_no,
                    verdict.issues,
                    resolved=verdict.resolved,
                    approved=verdict.approved,
                )
        if verdict is None or verdict.error or verdict.approved:
            return report

        # Gate PASSED, reviewer REJECTED — the case that used to end here with
        # the objections going nowhere.
        if ledger is None or not ledger.has_open():
            report.blocked_reason = "reviewer rejected but left no actionable finding"
            _emit("review.rework.stop", {"reason": report.blocked_reason}, attempt=number)
            return report
        if rounds_left <= 0:
            return report  # out of rounds: the proposal stays retained, findings shown

        rounds_left -= 1
        round_no += 1
        open_n = len(ledger.open_findings())
        _emit(
            "review.rework",
            {"round": round_no, "of": review_rounds, "open": open_n},
            attempt=number,
        )
        reporter.note(f"rework round {round_no}/{review_rounds} — {open_n} open finding(s)")
        # Same clean-basis discipline as a gate failure: the rejected proposal
        # goes away and the next pass is judged on its own merits. Clearing the
        # ref matters — it points at an output that no longer exists.
        output.discard()
        report.succeeded = False
        report.final_run_ref = None
        report.entries = None
        guidance = _prior_attempt_guidance(entries) + _format_guidance("review", ledger=ledger)
        attempts_left = max_attempts  # a round carries its own allowance

    return report
