"""CLI for supervised AI development on top of Shepherd.

Usage:
    # develop (output is held for review, nothing touches your files)
    shepherd-dev run "add CPF validation to signup" \
        --repo ~/projetos/foo --test-cmd "npm test"

    # settle a passing proposal (human decision)
    shepherd-dev settle <run-ref> --repo ~/projetos/foo            # accept
    shepherd-dev settle <run-ref> --repo ~/projetos/foo --reject   # discard

The target repo must be a Shepherd workspace (`shepherd init` inside it once).
Accepting a proposal advances the Shepherd world (select) AND writes the files
into your working tree, keeping both in sync; committing to git stays with you.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import shepherd as sp

from . import config, history, memory as repo_memory
from .contextpack import build_pack
from .parallel import develop_best_of, develop_parallel
from .policy import ChangesetPolicy
from .staging import PROPOSALS_DIR
from .supervisor import develop, materialize_into, read_changeset_entries, set_worker_budget
from .tasks import implement, review, write_tests


def _resolve_repo(raw: str | None) -> Path | None:
    """Resolve the target repo. raw=None (or '.') means: find the enclosing
    Shepherd workspace by walking up from the cwd, like git finds .git."""
    if raw in (None, "."):
        found = config.find_repo_root()
        if found is None:
            print(
                "error: not inside a Shepherd workspace. cd into an initialized repo, "
                "pass --repo <path>, or run `shepherd-dev init` first.",
                file=sys.stderr,
            )
            return None
        return found
    repo_root = Path(raw).expanduser().resolve()
    if not repo_root.is_dir():
        print(f"error: repo not found: {repo_root}", file=sys.stderr)
        return None
    if not (repo_root / ".vcscore").exists():
        print(
            f"error: {repo_root} is not a Shepherd workspace. Run once inside it: shepherd-dev init",
            file=sys.stderr,
        )
        return None
    return repo_root


def _resolve_test_cmd(repo_root: Path, explicit: str | None) -> tuple[str | None, str | None]:
    """Resolve the gate. Precedence: --test-cmd > saved config > project
    detection > native-gate fallback (zero-dep runner + a hint that makes the
    worker write its own tests). Prints the source; returns (cmd, worker_hint).
    (None, None) on failure."""
    cmd, source, hint = config.resolve_test_cmd(repo_root, explicit)
    if cmd is None:
        print(
            "error: no test command and no recognized language for a native gate. "
            "Pass --test-cmd \"…\" or save one with `shepherd-dev init --test-cmd \"…\"`.",
            file=sys.stderr,
        )
        return None, None
    if source == "detected":
        print(f"test gate (auto-detected): {cmd}")
    elif source == "config":
        print(f"test gate (from .shepherd-dev.json): {cmd}")
    elif source == "native":
        print(f"test gate (no suite found — using native runner, worker will write tests): {cmd}")
    return cmd, hint


def _with_hint(feature: str, hint: str | None) -> str:
    """Fold the native-gate hint into the feature request so the worker writes
    its own tests when the repo has no suite."""
    return f"{feature}\n\n{hint}" if hint else feature


def _resolve_gate(repo_root: Path, explicit_test_cmd: str | None, provider: str):
    """Resolve the gate, remote-aware. Returns (test_cmd, gate_hint, ok).

    When the repo configures a remote gate (test_remote), preflight the remote
    BEFORE any worker runs (fail loud on an unreachable host instead of burning
    attempts) and let the remote config carry its own test_cmd."""
    remote_cfg = config.remote_gate(repo_root)
    # static is offline; grok uses the same gate infrastructure as claude (local or remote)
    if remote_cfg is not None and provider != "static":
        from .remotegate import preflight

        print(f"remote gate: {remote_cfg.ssh}:{remote_cfg.repo_dir} — {remote_cfg.test_cmd}")
        err = preflight(remote_cfg)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return None, None, False
        return remote_cfg.test_cmd, None, True
    cmd, hint = _resolve_test_cmd(repo_root, explicit_test_cmd)
    return cmd, hint, cmd is not None


_ADOPT_KEY_FILE = ".shepherd-adopt-key"


def _adoption_key(repo_root: Path) -> str | None:
    """Fingerprint of the worktree state the adoption depends on: HEAD sha +
    `git status --porcelain` + (mtime,size) of every dirty/untracked path.
    None = no usable git state → never cache (always re-adopt)."""
    import hashlib
    import subprocess

    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, timeout=10
        )
        if head.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=repo_root, capture_output=True, text=True, timeout=30,
        )
        if status.returncode != 0:
            return None
        digest = hashlib.sha256()
        digest.update(head.stdout.encode())
        digest.update(status.stdout.encode())
        for entry in status.stdout.split("\0"):
            if len(entry) > 3:
                rel = entry[3:]
                try:
                    st = (repo_root / rel).stat()
                    digest.update(f"{rel}:{st.st_mtime_ns}:{st.st_size}".encode())
                except OSError:
                    digest.update(f"{rel}:gone".encode())
        return digest.hexdigest()
    except Exception:
        return None


def _refresh_substrate(repo_root: Path, fresh: bool = False) -> str | None:
    """Recreate .vcscore so the run basis equals the current worktree.

    In shepherd-ai 0.3.0 runs fork from the workspace's ORIGINAL adoption
    basis; settlements do not feed later runs' bases (verified empirically).
    Since git is our durable source of truth, each `shepherd-dev run` re-adopts the
    worktree from scratch — EXCEPT when the worktree provably hasn't changed
    since the last adoption (same HEAD + same dirty state, keyed by
    _adoption_key), where the multi-second re-adopt is skipped. Conservative:
    any doubt (no git, key mismatch, missing key file, ``fresh=True``)
    re-adopts. Refuses if an unconsumed proposal is still pending.
    Returns an error message, or None on success.
    """
    import shutil
    import subprocess

    vcscore = repo_root / ".vcscore"
    key = None if fresh else _adoption_key(repo_root)
    key_file = vcscore / _ADOPT_KEY_FILE
    if vcscore.exists():
        with sp.open(repo_root) as workspace:
            pending = []
            for record in workspace.runs.list():
                for output in workspace.runs.outputs(run_ref=record.run_ref):
                    if output.state == "unconsumed":
                        pending.append(record.run_ref)
        if pending:
            return (
                "pending unconsumed proposal(s): "
                + ", ".join(sorted(set(pending)))
                + " — settle them first (shepherd-dev settle <ref> [--reject])"
            )
        if key is not None:
            try:
                if key_file.is_file() and key_file.read_text(encoding="utf-8").strip() == key:
                    return None  # worktree unchanged since the last adoption
            except Exception:
                pass  # unreadable key: fall through to a fresh adoption
        shutil.rmtree(vcscore)

    shepherd_bin = Path(sys.executable).parent / "shepherd"
    proc = subprocess.run(
        [str(shepherd_bin), "init"], cwd=repo_root, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return f"shepherd init failed: {proc.stderr.strip() or proc.stdout.strip()}"
    if key is not None:
        try:
            (repo_root / ".vcscore" / _ADOPT_KEY_FILE).write_text(key, encoding="utf-8")
        except Exception:
            pass  # no key persisted → next run re-adopts (safe)
    return None


DIFF_PREVIEW_LINES = 60


def _read_run_entries(repo_root: Path, run_ref: str) -> dict[str, bytes]:
    """Read a retained run's proposed files WITHOUT consuming the output."""
    with sp.open(repo_root) as workspace:
        outs = [o for o in workspace.runs.outputs(run_ref=run_ref) if o.output_name == "workspace"]
        if not outs:
            return {}
        return read_changeset_entries(outs[0].changeset())


def _print_diff(entries: dict[str, bytes]) -> None:
    if not entries:
        print("  (no readable proposed files)")
        return
    for rel, content in entries.items():
        text = content.decode("utf-8", errors="replace")
        lines = text.splitlines()
        print(f"\n--- {rel} ({len(lines)} lines) ---")
        for ln in lines[:DIFF_PREVIEW_LINES]:
            print("  " + ln)
        if len(lines) > DIFF_PREVIEW_LINES:
            print(f"  … (+{len(lines) - DIFF_PREVIEW_LINES} more lines)")


def _ask_decision(prompt: str) -> str:
    """Return 'accept' | 'reject' | 'diff' | 'keep'. Empty/EOF => 'keep'."""
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "keep"
    if ans in ("a", "aceitar", "accept", "y", "s", "sim"):
        return "accept"
    if ans in ("r", "rejeitar", "reject", "n", "nao", "não"):
        return "reject"
    if ans in ("d", "diff", "ver"):
        return "diff"
    return "keep"


def _interactive_settle_run(repo_root: Path, run_ref: str) -> int:
    """Prompt accept/reject/diff for a single run; act inline."""
    while True:
        choice = _ask_decision("\nAceitar (a), rejeitar (r) ou ver o diff (d)? [a/r/d]: ")
        if choice == "diff":
            _print_diff(_read_run_entries(repo_root, run_ref))
            continue
        if choice == "accept":
            code, written = settle_run(repo_root, run_ref, reject=False)
            if code == 0 and written:
                print("revise e comite no git quando quiser.")
            return code
        if choice == "reject":
            code, _ = settle_run(repo_root, run_ref, reject=True)
            return code
        print(f"deixado retido — decida depois:\n  shepherd-dev settle {run_ref} --repo {repo_root} [--reject]")
        return 0


def _interactive_settle_proposal(repo_root: Path, proposal_id: str) -> int:
    """Prompt accept/reject/diff for a staged (run2/best-of) proposal; act inline."""
    files_dir = repo_root / PROPOSALS_DIR / proposal_id / "files"
    while True:
        choice = _ask_decision("\nAceitar (a), rejeitar (r) ou ver o diff (d)? [a/r/d]: ")
        if choice == "diff":
            entries = {
                str(p.relative_to(files_dir)): p.read_bytes()
                for p in files_dir.rglob("*") if p.is_file()
            } if files_dir.is_dir() else {}
            _print_diff(entries)
            continue
        if choice == "accept":
            code, written = settle_proposal(repo_root, proposal_id, reject=False)
            if code == 0 and written:
                print("revise e comite no git quando quiser.")
            return code
        if choice == "reject":
            code, _ = settle_proposal(repo_root, proposal_id, reject=True)
            return code
        print(f"deixado staged — decida depois:\n  shepherd-dev settle-par {proposal_id} --repo {repo_root} [--reject]")
        return 0


def _slugify(text: str, limit: int = 28) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "feature"


def auto_commit_branch(repo_root: Path, written: list[str], slug: str, message: str) -> tuple[str | None, str | None]:
    """Commit accepted files on an isolated shepherd/<slug> branch, then return
    to the original branch. Never pushes. Returns (branch_name, error)."""
    import subprocess

    def git(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *argv], cwd=repo_root, capture_output=True, text=True)

    cur = git("rev-parse", "--abbrev-ref", "HEAD")
    original = cur.stdout.strip()
    if cur.returncode != 0 or original == "HEAD":
        return None, "not on a branch (detached HEAD) — files left in the working tree, commit manually"

    branch = f"shepherd/{slug}"
    n = 2
    while git("rev-parse", "--verify", "--quiet", branch).returncode == 0:
        branch = f"shepherd/{slug}-{n}"
        n += 1

    # Sequential (NOT an eager list): if `checkout -b` fails, we must NOT run
    # add/commit — they would land on the user's current branch. And whatever
    # happens, always return to the original branch (#8).
    co = git("checkout", "-b", branch)
    if co.returncode != 0:
        return None, f"git checkout -b failed: {(co.stderr or co.stdout).strip()[:300]}"
    try:
        add = git("add", "--", *written)
        if add.returncode != 0:
            return None, f"git add failed: {(add.stderr or add.stdout).strip()[:300]}"
        commit = git("commit", "-m", message)
        if commit.returncode != 0:
            return None, f"git commit failed: {(commit.stderr or commit.stdout).strip()[:300]}"
        return branch, None
    finally:
        # unstage + restore the original branch even on a mid-way failure; the
        # accepted files remain in the worktree either way (they were already
        # materialized by settle before commit).
        git("reset", "-q")
        git("checkout", original)


def _auto_settle_conditions(report) -> str | None:
    """None = all hard conditions met; otherwise the human-readable reason."""
    if not report.succeeded:
        return "run did not succeed"
    if report.review is None:
        return "no review was run"
    if report.review.error:
        return f"review unavailable: {report.review.error}"
    if not report.review.approved:
        return "review REJECTED the proposal"
    return None


def _run_best_of(args, repo_root: Path, worker, reviewer, policy, placement, feature: str, pack: str | None = None, pack_stats: dict | None = None) -> int:
    # Verbose (best-of): one event log per candidate, replayed via `trace` —
    # no live rendering (K interleaved candidates would garble one spinner).
    event_logs = stream_hook = None
    if getattr(args, "verbose", False) and not getattr(args, "quiet", False):
        from .events import RunEventLog, WorkerStreamHook, new_run_id, repo_baseline_reader

        base = new_run_id()
        event_logs = [RunEventLog(run_id=f"{base}-c{i}") for i in range(args.best_of)]
        stream_hook = WorkerStreamHook(read_baseline=repo_baseline_reader(repo_root))
        if args.provider == "claude":
            set_worker_budget(args.worker_budget, stream_hook=stream_hook)
        print(f"verbose: per-candidate events → {event_logs[0].root}/{base}-c*/events.ndjson", file=sys.stderr)
        print(f"trace: shepherd-dev trace {base}-c0  (…-c{args.best_of - 1})", file=sys.stderr)

    report = develop_best_of(
        repo_root,
        feature,
        k=args.best_of,
        test_cmd=args.test_cmd,
        provider=args.provider,
        placement=placement,
        policy=policy,
        max_attempts=args.max_attempts,
        gate_timeout=args.gate_timeout,
        review_task=reviewer,
        worker_task=worker,
        context_pack=pack,
        event_logs=event_logs,
        stream_hook=stream_hook,
    )
    if report.succeeded:
        learned = repo_memory.learn_from_review(repo_root, report.review, report.proposal_id)
        if learned:
            print(f"repo memory: +{learned} fact(s) learned")
    history.record_event(
        "best_of",
        {
            "feature": args.feature,
            "repo": str(repo_root),
            "k": args.best_of,
            "succeeded": report.succeeded,
            "winner": report.winner_index,
            "proposal_id": report.proposal_id,
            "candidates": [
                {
                    "index": c.index,
                    "verdict": c.verdict,
                    "gate_passed": c.gate_passed,
                    "review_approved": (c.review.approved if c.review else None),
                    "files": c.files,
                    "diff_bytes": c.diff_bytes,
                }
                for c in report.candidates
            ],
            "flags": {"auto_settle": args.auto_settle, "mode": args.mode, "pack": pack_stats or None},
        },
    )
    print(report.summary())

    if args.auto_settle and report.proposal_id:
        reason = _auto_settle_conditions(report)
        if reason:
            print(f"\nauto-settle: NOT applied ({reason}) — proposal stays staged for manual settlement")
            return 0 if report.succeeded else 1
        code, written = settle_proposal(repo_root, report.proposal_id, reject=False, auto=True)
        if code != 0 or not written:
            return 1
        branch, err = auto_commit_branch(
            repo_root, written, _slugify(args.feature),
            f"feat: {args.feature}\n\nshepherd-dev auto-settle (best-of-{args.best_of}, "
            f"proposal {report.proposal_id}); gate passed, review approved.",
        )
        if err:
            print(f"auto-settle: files written but NOT committed — {err}")
        else:
            print(f"auto-settle: committed on branch {branch} (current branch untouched, no push)")
        return 0

    if _wants_interactive(args) and report.proposal_id:
        return _interactive_settle_proposal(repo_root, report.proposal_id)
    return 0 if report.succeeded else 1


def _run_planning(args, repo_root: Path, feature_text: str) -> tuple[tuple[str, ...], str]:
    """Cheap-model planning prefetch (#4). Returns (planned_targets, plan_text).

    Best-effort: disabled by --no-plan / static / config, and any failure returns
    empty so the run proceeds on keyword-scored targets exactly as before."""
    if getattr(args, "no_plan", False) or args.provider == "static":
        return (), ""
    from . import config as _config
    from .contextpack import repo_file_view
    from .planning import plan_targets

    cfg = _config.planning_config(repo_root)
    if not cfg["enabled"]:
        return (), ""
    tree_text, repo_rels = repo_file_view(repo_root, tuple(args.allowed_prefix))
    if not repo_rels:
        return (), ""
    result = plan_targets(feature_text, tree_text, repo_rels, model=cfg["model"])
    if result.error:
        print(f"planning: skipped ({result.error})")
        return (), ""
    print(f"planning: {len(result.targets)} target(s) via {cfg['model']}")
    return tuple(result.targets), result.plan


def _build_pack(args, repo_root: Path, feature_text: str) -> tuple[str | None, dict]:
    """Build the context pack (+ repo memory + planning prefetch) once per command.
    Disabled by --no-context-pack or on the static provider (no LLM to feed)."""
    if getattr(args, "no_context_pack", False) or args.provider == "static":
        return None, {}
    planned, plan_text = _run_planning(args, repo_root, feature_text)
    pack, stats = build_pack(
        repo_root,
        feature_text,
        allowed_prefixes=tuple(args.allowed_prefix),
        memory_text=repo_memory.memory_text(repo_root),
        planned_targets=planned,
        plan_text=plan_text,
    )
    print(
        f"context pack: {stats['chars']} chars "
        f"({stats['files_full']} full + {stats['files_skeleton']} skeleton files, "
        f"{stats['scanned']} scanned"
        + (f", {stats['planned']} planned" if stats.get('planned') else "")
        + ")"
    )
    return pack, stats


def _wants_interactive(args) -> bool:
    """Prompt inline only when asked (or by default on a TTY) and auto-settle
    is off. Never prompt when stdin is not a terminal (CI, subprocess replay)."""
    if getattr(args, "auto_settle", False) or getattr(args, "no_settle", False):
        return False
    if not sys.stdin.isatty():
        return False
    return getattr(args, "interactive", None) is not False  # default on for a TTY


def _record_optimize_event(report, *, auto: bool, applied: bool) -> None:
    history.record_event(
        "optimize",
        {
            "auto": auto,
            "accepted": report.accepted,
            "applied": applied and report.accepted,
            "candidate_key": (report.candidate.key if report.candidate else None),
            "fix": [report.fix_before, report.fix_after],
            "guard": [report.guard_before, report.guard_after],
            "reason": report.reason,
        },
    )


def _maybe_optimize_after(args, repo_root: Path) -> None:
    """Post-run optimize trigger (both layers, user-approved design):

    - --optimize-after forces one optimize pass after this run
      (--optimize-apply persists a passing edit);
    - otherwise the auto_optimize config ({"every_failures": N, "apply": bool}
      in .shepherd-dev.json or ~/.shepherd-dev/config.json) fires only once N
      claude-run failures accumulate since the last optimize — cost stays
      controlled because optimize replays real cases (~7 Claude sessions).
    Never raises: a failed optimize must not change the run's exit code.
    """
    if args.provider in ("static", "grok", "codex"):
        # optimize meta-prompt is Claude-CLI based today; never run it on grok/codex/static paths
        return
    forced = getattr(args, "optimize_after", False)
    apply_edit = getattr(args, "optimize_apply", False)
    if not forced:
        cfg = config.auto_optimize_config(repo_root)
        if not cfg:
            return
        try:
            every = int(cfg.get("every_failures", 5))
        except (TypeError, ValueError):
            every = 5
        pending = history.failures_since_last_optimize()
        if pending < max(every, 1):
            return
        apply_edit = bool(cfg.get("apply", False))
        print(f"\nauto-optimize: {pending} failure(s) accumulated since the last optimize — running it now")
    else:
        print("\noptimize-after: running optimize as requested")
    try:
        from .optimize import optimize

        report = optimize(apply=apply_edit, worker_budget=getattr(args, "worker_budget", 900))
        print(report.summary())
        _record_optimize_event(report, auto=not forced, applied=apply_edit)
    except Exception as exc:
        print(f"optimize failed (run result unaffected): {type(exc).__name__}: {exc}", file=sys.stderr)


def cmd_run(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2
    try:
        return _cmd_run_inner(args, repo_root)
    finally:
        _maybe_optimize_after(args, repo_root)


def _cmd_run_inner(args, repo_root: Path) -> int:

    if args.auto_settle and args.no_review:
        print("error: --auto-settle requires the reviewer (drop --no-review)", file=sys.stderr)
        return 2
    if args.auto_settle and args.provider in ("static",):
        print("error: --auto-settle requires a reviewing provider (not static)", file=sys.stderr)
        return 2
    if args.auto_settle and args.provider in ("grok", "codex") and args.no_review:
        print(f"error: --auto-settle with {args.provider} requires review (drop --no-review)", file=sys.stderr)
        return 2

    if args.best_of > 1 and args.no_review and args.provider not in ("static",):
        # best-of ranking needs review for non-static; grok/codex best-of not supported yet
        if args.provider not in ("grok", "codex"):
            print("error: --best-of needs the reviewer for ranking (drop --no-review)", file=sys.stderr)
            return 2
    if args.provider in ("grok", "codex") and args.best_of > 1:
        print(f"error: --best-of is not supported with --provider {args.provider} yet (use claude)", file=sys.stderr)
        return 2

    args.test_cmd, gate_hint, ok = _resolve_gate(repo_root, args.test_cmd, args.provider)
    if not ok:
        return 2
    feature = _with_hint(args.feature, gate_hint)
    pack, pack_stats = _build_pack(args, repo_root, args.feature)

    # ── Hosted paths (L1 host / L2 try): no Claude, no workspace.run by default ──
    if args.provider in ("grok", "codex"):
        return _cmd_run_hosted(args, repo_root, feature, pack, pack_stats, provider=args.provider)

    error = _refresh_substrate(repo_root, fresh=getattr(args, "fresh_adopt", False))
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    # Verbose mode: a per-run event log + the worker stream hook (tee/tailer).
    # The hook rides set_worker_budget's transport rebind, so it exists only on
    # the claude provider; the gate/review events work on every provider.
    event_log = stream_hook = None
    if getattr(args, "verbose", False) and not getattr(args, "quiet", False) and args.best_of == 1:
        from .events import RunEventLog, WorkerStreamHook, repo_baseline_reader

        event_log = RunEventLog()
        stream_hook = WorkerStreamHook(event_log, read_baseline=repo_baseline_reader(repo_root))
        print(f"verbose: events → {event_log.path}", file=sys.stderr)

    if args.provider == "claude":
        set_worker_budget(args.worker_budget, stream_hook=stream_hook)

    policy = ChangesetPolicy(
        max_changed_paths=args.max_changed_paths,
        allowed_prefixes=tuple(args.allowed_prefix),
    )
    placement = "jail" if args.provider == "claude" else "advisory"
    worker = implement if args.mode == "feature" else write_tests
    # reviewer needs a live model; skip it on the deterministic provider
    reviewer = None if (args.no_review or args.provider == "static") else review

    if args.best_of > 1:
        return _run_best_of(args, repo_root, worker, reviewer, policy, placement, feature, pack, pack_stats)

    from .progress import NullProgress, ProgressReporter, VerboseReporter

    if getattr(args, "quiet", False):
        reporter = NullProgress()
    elif event_log is not None:
        reporter = VerboseReporter()
        event_log.subscribe(reporter.handle_event)
    else:
        reporter = ProgressReporter()
    with sp.open(repo_root) as workspace:
        report = develop(
            workspace,
            worker,
            repo=workspace.git_repo(),
            repo_root=repo_root,
            feature=feature,
            test_cmd=args.test_cmd,
            provider=args.provider,
            placement=placement,
            max_attempts=args.max_attempts,
            gate_timeout=args.gate_timeout,
            policy=policy,
            review_task=reviewer,
            context_pack=pack,
            reporter=reporter,
            worker_budget=(None if getattr(args, "no_watchdog", False) else args.worker_budget),
            event_log=event_log,
            stream_hook=stream_hook,
            speculative_review=getattr(args, "speculative_review", False),
        )
    reporter.close(ok=report.succeeded)
    if event_log is not None:
        event_log.emit(
            "run.summary",
            {
                "succeeded": report.succeeded,
                "attempts": len(report.attempts),
                "final_run_ref": report.final_run_ref,
                "feature": args.feature,
            },
        )
        print(f"trace: shepherd-dev trace {event_log.run_id}", file=sys.stderr)

    learned = repo_memory.learn_from_report(repo_root, report)
    if learned:
        print(f"repo memory: +{learned} fact(s) learned")
    history.record_event(
        "run",
        history.run_payload(
            report, repo_root,
            mode=args.mode, test_cmd=args.test_cmd, provider=args.provider,
            flags={
                "max_attempts": args.max_attempts,
                "allowed_prefix": args.allowed_prefix,
                "auto_settle": args.auto_settle,
                "pack": pack_stats or None,
                "verbose_run": event_log.run_id if event_log is not None else None,
            },
        ),
    )
    print(report.summary())

    if args.auto_settle and report.final_run_ref:
        reason = _auto_settle_conditions(report)
        if reason:
            print(f"\nauto-settle: NOT applied ({reason}) — proposal stays retained for manual settlement")
            return 0 if report.succeeded else 1
        code, written = settle_run(repo_root, report.final_run_ref, reject=False, auto=True)
        if code != 0 or not written:
            return 1
        branch, err = auto_commit_branch(
            repo_root, written, _slugify(args.feature),
            f"feat: {args.feature}\n\nshepherd-dev auto-settle ({report.final_run_ref}); "
            f"gate passed, review approved.",
        )
        if err:
            print(f"auto-settle: files written but NOT committed — {err}")
        else:
            print(f"auto-settle: committed on branch {branch} (current branch untouched, no push)")
        return 0

    if _wants_interactive(args) and report.final_run_ref:
        # The gate passing is NOT approval: warn loudly if the reviewer rejected,
        # so accepting isn't a rubber-stamp of a REJECTED proposal (#12).
        rev = report.review
        if rev is not None and not rev.error and not rev.approved:
            print(f"\n⚠ reviewer REJECTED this proposal — {rev.summary}", file=sys.stderr)
            for issue in rev.issues:
                print(f"    issue: {issue}", file=sys.stderr)
        return _interactive_settle_run(repo_root, report.final_run_ref)
    return 0 if report.succeeded else 1


def _cmd_run_hosted(
    args, repo_root: Path, feature: str, pack: str | None, pack_stats: dict, *, provider: str
) -> int:
    """Hosted provider path (grok/codex): L1 host (default) / L2 try — no Claude subprocess."""
    from .progress import NullProgress, ProgressReporter

    policy = ChangesetPolicy(
        max_changed_paths=args.max_changed_paths,
        allowed_prefixes=tuple(args.allowed_prefix),
    )
    # Hosted providers do not need _refresh_substrate (settlement is stage/settle-par).
    prefer_lane = getattr(args, "worker_backend", "auto") in ("lane", "auto")
    if getattr(args, "worker_backend", "auto") == "host":
        prefer_lane = False

    reporter = NullProgress() if getattr(args, "quiet", False) else ProgressReporter()
    do_review = not args.no_review
    if provider == "codex":
        from .providers.codex_lane import develop_codex_lane_or_host

        report = develop_codex_lane_or_host(
            repo_root,
            feature,
            test_cmd=args.test_cmd,
            prefer_lane=prefer_lane,
            max_attempts=args.max_attempts,
            gate_timeout=args.gate_timeout,
            worker_budget=args.worker_budget,
            policy=policy,
            context_pack=pack,
            mode=args.mode,
            do_review=do_review,
            codex_bin=getattr(args, "codex_cmd", None),
            model=getattr(args, "codex_model", None),
            reporter=reporter,
        )
    else:
        from .providers.grok_lane import develop_grok_lane_or_host

        report = develop_grok_lane_or_host(
            repo_root,
            feature,
            test_cmd=args.test_cmd,
            prefer_lane=prefer_lane,
            max_attempts=args.max_attempts,
            gate_timeout=args.gate_timeout,
            worker_budget=args.worker_budget,
            policy=policy,
            context_pack=pack,
            mode=args.mode,
            do_review=do_review,
            grok_bin=getattr(args, "grok_cmd", None),
            model=getattr(args, "grok_model", None),
            reporter=reporter,
        )
    reporter.close(ok=report.succeeded)

    dev = report.as_dev_report()
    learned = repo_memory.learn_from_report(repo_root, dev)
    if learned:
        print(f"repo memory: +{learned} fact(s) learned")
    history.record_event(
        "run",
        {
            **history.run_payload(
                dev, repo_root,
                mode=args.mode, test_cmd=args.test_cmd, provider=provider,
                flags={
                    "max_attempts": args.max_attempts,
                    "allowed_prefix": args.allowed_prefix,
                    "auto_settle": args.auto_settle,
                    "pack": pack_stats or None,
                    "backend": report.backend,
                    "proposal_id": report.proposal_id,
                },
            ),
            "proposal_id": report.proposal_id,
        },
    )
    print(report.summary())

    if args.auto_settle and report.proposal_id:
        reason = _auto_settle_conditions(dev) if do_review else None
        if not do_review:
            reason = "no review was run"
        if reason:
            print(f"\nauto-settle: NOT applied ({reason}) — proposal stays staged for manual settlement")
            return 0 if report.succeeded else 1
        code, written = settle_proposal(repo_root, report.proposal_id, reject=False, auto=True)
        if code != 0 or not written:
            return 1
        branch, err = auto_commit_branch(
            repo_root, written, _slugify(args.feature),
            f"feat: {args.feature}\n\nshepherd-dev auto-settle {provider} "
            f"(proposal {report.proposal_id}); gate passed, review approved.",
        )
        if err:
            print(f"auto-settle: files written but NOT committed — {err}")
        else:
            print(f"auto-settle: committed on branch {branch} (current branch untouched, no push)")
        return 0

    if _wants_interactive(args) and report.proposal_id:
        rev = report.review
        if rev is not None and not rev.error and not rev.approved:
            print(f"\n⚠ reviewer REJECTED this proposal — {rev.summary}", file=sys.stderr)
            for issue in rev.issues:
                print(f"    issue: {issue}", file=sys.stderr)
        return _interactive_settle_proposal(repo_root, report.proposal_id)
    return 0 if report.succeeded else 1


def cmd_run2(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2
    try:
        return _cmd_run2_inner(args, repo_root)
    finally:
        _maybe_optimize_after(args, repo_root)


def _cmd_run2_inner(args, repo_root: Path) -> int:

    if args.auto_settle and args.no_review:
        print("error: --auto-settle requires the reviewer (drop --no-review)", file=sys.stderr)
        return 2
    if args.auto_settle and args.provider == "static":
        print("error: --auto-settle requires the claude provider (review is mandatory)", file=sys.stderr)
        return 2

    args.test_cmd, gate_hint, ok = _resolve_gate(repo_root, args.test_cmd, args.provider)
    if not ok:
        return 2
    pack, pack_stats = _build_pack(args, repo_root, f"{args.feature_a} {args.feature_b}")

    # Verbose (run2): one event log per worker (-wa/-wb, streams + develop
    # events; the two run concurrently, so no live rendering for them) plus a
    # MAIN log with the run2 narrative — conflicts/handoff, the streamed
    # combined gate, repair rounds, review. The main log renders live (those
    # phases are sequential); replay everything with `trace`.
    event_logs = event_log_main = stream_hook = None
    if getattr(args, "verbose", False):
        from .events import RunEventLog, WorkerStreamHook, new_run_id, repo_baseline_reader
        from .progress import VerboseReporter

        base = new_run_id()
        event_logs = [RunEventLog(run_id=f"{base}-wa"), RunEventLog(run_id=f"{base}-wb")]
        event_log_main = RunEventLog(run_id=base)
        stream_hook = WorkerStreamHook(read_baseline=repo_baseline_reader(repo_root))
        event_log_main.subscribe(VerboseReporter().handle_event)
        print(f"verbose: events → {event_log_main.root}/{base}*/events.ndjson", file=sys.stderr)
        print(f"trace: shepherd-dev trace {base}  (workers: {base}-wa, {base}-wb)", file=sys.stderr)

    if args.provider == "claude":
        set_worker_budget(args.worker_budget, stream_hook=stream_hook)

    policy = ChangesetPolicy(
        max_changed_paths=args.max_changed_paths,
        allowed_prefixes=tuple(args.allowed_prefix),
    )
    placement = "jail" if args.provider == "claude" else "advisory"
    reviewer = None if (args.no_review or args.provider == "static") else review

    report = develop_parallel(
        repo_root,
        [_with_hint(args.feature_a, gate_hint), _with_hint(args.feature_b, gate_hint)],
        test_cmd=args.test_cmd,
        context_pack=pack,
        provider=args.provider,
        placement=placement,
        policy=policy,
        max_attempts=args.max_attempts,
        max_repairs=args.max_repairs,
        gate_timeout=args.gate_timeout,
        review_task=reviewer,
        event_logs=event_logs,
        event_log_main=event_log_main,
        stream_hook=stream_hook,
    )
    if event_log_main is not None:
        event_log_main.emit(
            "run.summary",
            {
                "succeeded": report.succeeded,
                "final_run_ref": report.proposal_id,
                "conflicts": report.conflicts,
                "repairs": report.repairs,
            },
        )
    if report.succeeded:
        learned = repo_memory.learn_from_review(repo_root, report.review, report.proposal_id)
        if learned:
            print(f"repo memory: +{learned} fact(s) learned")
    history.record_event(
        "run2",
        history.parallel_payload(
            report, repo_root,
            test_cmd=args.test_cmd, provider=args.provider,
            flags={
                "max_attempts": args.max_attempts,
                "max_repairs": args.max_repairs,
                "auto_settle": args.auto_settle,
                "pack": pack_stats or None,
                "verbose_run": event_log_main.run_id if event_log_main is not None else None,
            },
        ),
    )
    print(report.summary())

    if args.auto_settle and report.proposal_id:
        reason = _auto_settle_conditions(report)
        if reason:
            print(f"\nauto-settle: NOT applied ({reason}) — proposal stays staged for manual settlement")
            return 0 if report.succeeded else 1
        code, written = settle_proposal(repo_root, report.proposal_id, reject=False, auto=True)
        if code != 0 or not written:
            return 1
        branch, err = auto_commit_branch(
            repo_root, written, _slugify(f"{args.feature_a}-{args.feature_b}"),
            f"feat: {args.feature_a} + {args.feature_b}\n\nshepherd-dev auto-settle "
            f"(proposal {report.proposal_id}); combined gate passed, review approved.",
        )
        if err:
            print(f"auto-settle: files written but NOT committed — {err}")
        else:
            print(f"auto-settle: committed on branch {branch} (current branch untouched, no push)")
        return 0

    if _wants_interactive(args) and report.proposal_id:
        return _interactive_settle_proposal(repo_root, report.proposal_id)
    return 0 if report.succeeded else 1


def settle_proposal(repo_root: Path, proposal_id: str, *, reject: bool, auto: bool = False) -> tuple[int, list[str]]:
    """Core settlement for a staged run2/best-of proposal. Returns (exit_code, written)."""
    import shutil

    staging = repo_root / PROPOSALS_DIR / proposal_id
    files_dir = staging / "files"
    if not files_dir.is_dir():
        print(f"error: staged proposal not found: {staging}", file=sys.stderr)
        return 2, []

    if reject:
        shutil.rmtree(staging)
        history.record_event(
            "settle_par",
            {"repo": str(repo_root), "ref": proposal_id, "action": "reject", "auto": auto},
        )
        print(f"{proposal_id}: staged proposal discarded")
        return 0, []

    # Skip symlinks (#18): a symlink planted in the stage could point outside and
    # copy an external file's content into the repo. Only real files are settled;
    # materialize_into still guards the destination against `..` escapes.
    entries = {
        str(path.relative_to(files_dir)): path.read_bytes()
        for path in files_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    written = materialize_into(repo_root, entries)
    shutil.rmtree(staging)
    history.record_event(
        "settle_par",
        {"repo": str(repo_root), "ref": proposal_id, "action": "accept", "auto": auto, "written": written},
    )
    print(f"{proposal_id}: accepted — {len(written)} file(s) written:")
    for rel in written:
        print(f"  {rel}")
    return 0, written


def cmd_settle_par(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2
    code, written = settle_proposal(repo_root, args.proposal_id, reject=args.reject)
    if code == 0 and written:
        print("review and commit them with git.")
    return code


GITIGNORE_ENTRIES = (".vcscore/", "REVIEW.json", ".shepherd-proposals/")


def _ensure_gitignore(repo_root: Path) -> list[str]:
    """Append the Shepherd state entries to the repo's .gitignore, once.

    Idempotent (skips lines already present). Returns the entries it added.
    """
    path = repo_root / ".gitignore"
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    present = {ln.strip() for ln in existing_lines}
    missing = [e for e in GITIGNORE_ENTRIES if e not in present]
    if not missing:
        return []
    block: list[str] = []
    if existing_lines and existing_lines[-1].strip():
        block.append("")
    block.append("# shepherd-dev local state")
    block.extend(missing)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(("\n".join(block)) + "\n")
    return missing


def cmd_init(args) -> int:
    """Initialize a repo as a Shepherd workspace AND gitignore its local state.

    The `shepherd` console script lives inside this tool's venv (dependency
    shepherd-ai) but is not exposed on the user's PATH by uv/pipx — only
    shepherd-dev is. This subcommand bridges that, then wires .gitignore so the
    user does not have to.
    """
    import subprocess

    repo_root = Path(args.repo or ".").expanduser().resolve()
    if not repo_root.is_dir():
        print(f"error: repo not found: {repo_root}", file=sys.stderr)
        return 2
    shepherd_bin = Path(sys.executable).parent / "shepherd"
    proc = subprocess.run([str(shepherd_bin), "init"], cwd=repo_root)
    if proc.returncode != 0:
        return proc.returncode

    if args.no_gitignore:
        print("skipped .gitignore (--no-gitignore); add: " + "  ".join(GITIGNORE_ENTRIES))
    else:
        added = _ensure_gitignore(repo_root)
        if added:
            print(f"gitignored: {'  '.join(added)}")
        else:
            print(".gitignore already covers the shepherd-dev state")

    # Elixir coverage guard: mix test needs the ExUnit scaffold. If a mix project
    # has no ExUnit set up, STOP and tell the user we are generating it, then do.
    if (repo_root / "mix.exs").is_file() and not config.exunit_ready(repo_root):
        print(
            "\nElixir project without ExUnit configured (no test/test_helper.exs "
            "calling ExUnit.start()).\nGenerating the minimal ExUnit scaffold so the "
            "`mix test` gate has somewhere to run:"
        )
        config.ensure_exunit_scaffold(repo_root)
        print("  created test/test_helper.exs  (ExUnit.start())")
        print("  note: a Phoenix `mix test` may also need deps (mix deps.get) and a "
              "test DB (mix ecto.create/migrate, MIX_ENV=test).")

    # Remember the test command so `run` needs no --test-cmd. Explicit flag is
    # saved as-is. Otherwise resolve the way `run` will: only persist a real,
    # runnable detected suite — a dead package-manager gate or a native fallback
    # is NOT saved, so `run` re-derives the native gate (with its test-writing
    # hint) every time.
    if args.test_cmd:
        config.save_config(repo_root, {"test_cmd": args.test_cmd})
        print(f"test gate (saved): {args.test_cmd}  →  {config.CONFIG_NAME}")
    else:
        cmd, source, _ = config.resolve_test_cmd(repo_root, None)
        if source == "detected":
            config.save_config(repo_root, {"test_cmd": cmd})
            print(f"test gate (detected & saved): {cmd}  →  {config.CONFIG_NAME}")
        elif source == "native":
            print(f"test gate: no suite found — `run` will use the native runner ({cmd}) and write tests itself")
        else:
            print("no test command — pass --test-cmd on run, or re-init with --test-cmd \"…\"")
    return 0


def settle_run(repo_root: Path, run_ref: str, *, reject: bool, auto: bool = False) -> tuple[int, list[str]]:
    """Core settlement for a retained run output. Returns (exit_code, written_paths)."""
    with sp.open(repo_root) as workspace:
        outputs = [
            o for o in workspace.runs.outputs(run_ref=run_ref) if o.output_name == "workspace"
        ]
        if not outputs:
            print(f"error: no workspace output found for {run_ref}", file=sys.stderr)
            return 2, []
        output = outputs[0]

        state = output.state
        if state != "unconsumed":
            print(
                f"error: {run_ref} output is already consumed (state={state!r}); "
                "settlement verbs are consume-once",
                file=sys.stderr,
            )
            return 2, []

        if reject:
            output.discard()
            history.record_event(
                "settle", {"repo": str(repo_root), "ref": run_ref, "action": "reject", "auto": auto}
            )
            print(f"{run_ref}: proposal discarded")
            return 0, []

        # Snapshot the changeset BEFORE selecting (settlement consumes the output).
        entries = read_changeset_entries(output.changeset())
        if not entries:
            print(f"error: {run_ref} has an empty changeset; nothing to accept", file=sys.stderr)
            return 2, []
        output.select()

    # Mirror files only after the workspace closes: while it is active, vcs-core
    # blocks unscoped mutations of workspace files (UnscopedMutationError).
    # The output is already consumed (select). If writing the worktree now fails
    # (disk full, permission, path edge), the accepted content would be lost — so
    # dump the in-memory snapshot to a recovery dir instead of losing it (#3).
    try:
        written = materialize_into(repo_root, entries)
    except Exception as exc:
        recovery = repo_root / ".shepherd-proposals" / f"recovered-{run_ref}"
        try:
            recovered = materialize_into(recovery, entries)
        except Exception:
            recovered = []
        history.record_event(
            "settle",
            {"repo": str(repo_root), "ref": run_ref, "action": "accept_failed",
             "auto": auto, "error": str(exc), "recovered": len(recovered)},
        )
        print(f"error: {run_ref} was consumed but writing to the worktree failed: {exc}", file=sys.stderr)
        if recovered:
            print(f"  the accepted content was saved under {recovery} — move the files "
                  "into place manually (the proposal cannot be re-settled)", file=sys.stderr)
        return 2, []

    history.record_event(
        "settle",
        {"repo": str(repo_root), "ref": run_ref, "action": "accept", "auto": auto, "written": written},
    )

    print(f"{run_ref}: accepted — world advanced, {len(written)} file(s) written:")
    for rel in written:
        print(f"  {rel}")
    return 0, written


def cmd_optimize(args) -> int:
    from .optimize import optimize

    report = optimize(
        fix_n=args.fix_n, guard_n=args.guard_n, model=args.model,
        worker_budget=args.worker_budget, apply=args.apply,
    )
    print(report.summary())
    _record_optimize_event(report, auto=False, applied=args.apply)
    return 0


def cmd_settle(args) -> int:
    repo_root = _resolve_repo(args.repo)
    if repo_root is None:
        return 2
    code, written = settle_run(repo_root, args.run_ref, reject=args.reject)
    if code == 0 and written:
        print("review and commit them with git.")
    return code


def cmd_mcp(args) -> int:
    """Run as an MCP stdio server so any MCP client (Codex, Cursor, Claude Code,
    the ChatGPT desktop app) can drive shepherd-dev as native tools."""
    from .mcpserver import serve

    return serve()


def cmd_trace(args) -> int:
    """Replay the step-by-step timeline of a run recorded by --verbose."""
    from .events import latest_run_id, load_run_events

    run_id = args.run_id
    if run_id in (None, "last"):
        run_id = latest_run_id()
        if run_id is None:
            print("no recorded runs (run with --verbose to record one)", file=sys.stderr)
            return 2
    events = load_run_events(run_id)
    if not events:
        print(f"no events for run {run_id}", file=sys.stderr)
        return 2
    if args.json:
        for event in events:
            print(json.dumps(event, ensure_ascii=False))
        return 0
    from .progress import render_trace

    print(f"run {run_id} · {len(events)} event(s)")
    for line in render_trace(events, full=args.full):
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Supervised AI development via Shepherd")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="develop a feature under supervision")
    p_run.add_argument("feature", help="feature request in natural language")
    p_run.add_argument("--repo", default=None, help="target repo (default: enclosing Shepherd workspace)")
    p_run.add_argument("--test-cmd", default=None, help='test gate; default: saved config, else auto-detected')
    p_run.add_argument(
        "--provider",
        default="claude",
        choices=["claude", "static", "grok", "codex"],
        help="worker backend: claude (default, jail), static (offline dry-run), "
             "grok / codex (no Claude — L1 host / L2 try; codex adds a real LLM review)",
    )
    p_run.add_argument(
        "--worker-backend",
        default="auto",
        choices=["auto", "host", "lane"],
        help="grok/codex only: host=isolated clone+CLI (L1); lane=try workspace rebind (L2) then host; auto=lane-ready host",
    )
    p_run.add_argument(
        "--grok-cmd",
        default=None,
        help="grok only: path to the Grok CLI (default: PATH or ~/.grok/bin/grok; env SHEPHERD_DEV_GROK_CMD)",
    )
    p_run.add_argument(
        "--grok-model",
        default=None,
        help="grok only: model id for the Grok CLI (env SHEPHERD_DEV_GROK_MODEL)",
    )
    p_run.add_argument(
        "--codex-cmd",
        default=None,
        help="codex only: path to the Codex CLI (default: PATH; env SHEPHERD_DEV_CODEX_CMD)",
    )
    p_run.add_argument(
        "--codex-model",
        default=None,
        help="codex only: model id for `codex exec -m` (env SHEPHERD_DEV_CODEX_MODEL)",
    )
    p_run.add_argument(
        "--mode",
        default="feature",
        choices=["feature", "tests"],
        help="feature: implement the request; tests: only write/update tests for it",
    )
    p_run.add_argument(
        "--no-review",
        action="store_true",
        help="skip the reviewer pass after the gate passes",
    )
    p_run.add_argument(
        "--auto-settle",
        action="store_true",
        help="on gate PASS + review APPROVED: settle and commit on an isolated shepherd/<slug> branch (never pushes)",
    )
    p_run.add_argument(
        "--no-settle",
        action="store_true",
        help="do not prompt to accept/reject at the end; just leave the proposal retained",
    )
    p_run.add_argument(
        "--no-context-pack",
        action="store_true",
        help="skip the pre-computed context pack (worker explores the repo itself)",
    )
    p_run.add_argument(
        "--no-plan",
        action="store_true",
        help="skip the cheap-model planning prefetch (no target/plan hints)",
    )
    p_run.add_argument(
        "--quiet",
        action="store_true",
        help="silence the live per-phase progress reporter",
    )
    p_run.add_argument(
        "-v", "--verbose",
        dest="verbose",
        action="store_true",
        default=True,
        help="live step-by-step feed: every worker tool call, per-edit diff, "
             "streamed gate line and named test failure; events persisted for "
             "`shepherd-dev trace` replay (DEFAULT — kept for compatibility)",
    )
    p_run.add_argument(
        "--no-verbose",
        dest="verbose",
        action="store_false",
        help="turn off the step-by-step feed (phase progress only, no event log)",
    )
    p_run.add_argument(
        "--no-watchdog",
        action="store_true",
        help="disable the worker budget hard-kill backstop (#B)",
    )
    p_run.add_argument(
        "--fresh-adopt",
        action="store_true",
        help="force a full re-adoption of the worktree (skip the unchanged-worktree cache)",
    )
    p_run.add_argument(
        "--speculative-review",
        action="store_true",
        help="run the reviewer in parallel with the gate (hides review latency; "
             "spends review tokens even when the gate fails)",
    )
    p_run.add_argument(
        "--optimize-after",
        action="store_true",
        help="run `optimize` after this run finishes (dry-run unless --optimize-apply)",
    )
    p_run.add_argument(
        "--optimize-apply",
        action="store_true",
        help="with --optimize-after (or auto_optimize): persist a passing prompt edit",
    )
    p_run.add_argument(
        "--best-of",
        type=int,
        default=1,
        choices=[1, 2, 3, 4],
        help="branch K candidates from the same state, gate+review all, stage the winner (Tree-RL essence at inference)",
    )
    p_run.add_argument("--max-attempts", type=int, default=3)
    p_run.add_argument("--gate-timeout", type=int, default=600, help="seconds for the test suite")
    p_run.add_argument(
        "--worker-budget",
        type=int,
        default=900,
        help="wall-clock seconds each worker attempt may use (claude provider)",
    )
    p_run.add_argument("--max-changed-paths", type=int, default=40)
    p_run.add_argument(
        "--allowed-prefix",
        action="append",
        default=[],
        help="restrict changes to this path prefix (repeatable)",
    )
    p_run.set_defaults(func=cmd_run)

    p_run2 = sub.add_parser("run2", help="develop two features with parallel coordinated workers")
    p_run2.add_argument("feature_a", help="first feature (leader on conflicts)")
    p_run2.add_argument("feature_b", help="second feature (reworks on conflicts)")
    p_run2.add_argument("--repo", default=None, help="target repo (default: enclosing Shepherd workspace)")
    p_run2.add_argument("--test-cmd", default=None, help="combined gate; default: saved config, else auto-detected")
    p_run2.add_argument(
        "--provider",
        default="claude",
        choices=["claude", "static"],
        help="claude (default) or static; grok parallel is not supported yet",
    )
    p_run2.add_argument("--no-review", action="store_true")
    p_run2.add_argument(
        "-v", "--verbose",
        dest="verbose",
        action="store_true",
        default=True,
        help="per-worker + combined event logs, streamed combined gate, trace replay (DEFAULT)",
    )
    p_run2.add_argument(
        "--no-verbose",
        dest="verbose",
        action="store_false",
        help="turn off the step-by-step event logs",
    )
    p_run2.add_argument(
        "--auto-settle",
        action="store_true",
        help="on combined gate PASS + review APPROVED: settle and commit on an isolated shepherd/<slug> branch (never pushes)",
    )
    p_run2.add_argument(
        "--no-settle",
        action="store_true",
        help="do not prompt to accept/reject at the end; just leave the proposal staged",
    )
    p_run2.add_argument(
        "--no-context-pack",
        action="store_true",
        help="skip the pre-computed context pack (workers explore the repo themselves)",
    )
    p_run2.add_argument(
        "--no-plan",
        action="store_true",
        help="skip the cheap-model planning prefetch (no target/plan hints)",
    )
    p_run2.add_argument(
        "--optimize-after",
        action="store_true",
        help="run `optimize` after this run finishes (dry-run unless --optimize-apply)",
    )
    p_run2.add_argument(
        "--optimize-apply",
        action="store_true",
        help="with --optimize-after (or auto_optimize): persist a passing prompt edit",
    )
    p_run2.add_argument("--max-attempts", type=int, default=2, help="attempts per worker")
    p_run2.add_argument("--max-repairs", type=int, default=2, help="repair rounds on the combined gate")
    p_run2.add_argument("--gate-timeout", type=int, default=600)
    p_run2.add_argument("--worker-budget", type=int, default=900)
    p_run2.add_argument("--max-changed-paths", type=int, default=40)
    p_run2.add_argument("--allowed-prefix", action="append", default=[])
    p_run2.set_defaults(func=cmd_run2)

    p_spar = sub.add_parser("settle-par", help="accept or reject a staged parallel proposal")
    p_spar.add_argument("proposal_id", help="staged proposal id (see run2 output)")
    p_spar.add_argument("--repo", default=None, help="target repo (default: enclosing Shepherd workspace)")
    p_spar.add_argument("--reject", action="store_true", help="discard instead of accept")
    p_spar.set_defaults(func=cmd_settle_par)

    p_settle = sub.add_parser("settle", help="accept or reject a retained proposal")
    p_settle.add_argument("run_ref", help="full run ref, e.g. run-fc83a2df3eaa")
    p_settle.add_argument("--repo", default=None, help="target repo (default: enclosing Shepherd workspace)")
    p_settle.add_argument("--reject", action="store_true", help="discard instead of accept")
    p_settle.set_defaults(func=cmd_settle)

    p_init = sub.add_parser("init", help="initialize a repo as a Shepherd workspace + gitignore its state (one-time)")
    p_init.add_argument("--repo", default=".", help="path to the target repo (default: cwd)")
    p_init.add_argument("--test-cmd", default=None, help="save this gate command (else auto-detect and save)")
    p_init.add_argument("--no-gitignore", action="store_true", help="do not touch .gitignore")
    p_init.set_defaults(func=cmd_init)

    p_opt = sub.add_parser("optimize", help="CRO-lite: mine run history, propose a prompt edit, validate by replay")
    p_opt.add_argument("--fix-n", type=int, default=3, help="past failures to replay (must improve)")
    p_opt.add_argument("--guard-n", type=int, default=3, help="past passes to replay (must not regress)")
    p_opt.add_argument("--model", default="claude-opus-4-8", help="meta-optimizer model")
    p_opt.add_argument("--worker-budget", type=int, default=900)
    p_opt.add_argument("--apply", action="store_true", help="persist the edit if it passes (default: dry-run)")
    p_opt.set_defaults(func=cmd_optimize)

    p_mcp = sub.add_parser("mcp", help="run as an MCP stdio server (Codex / Cursor / Claude Code / ChatGPT desktop)")
    p_mcp.set_defaults(func=cmd_mcp)

    p_trace = sub.add_parser("trace", help="replay a run's event timeline (verbose runs record one)")
    p_trace.add_argument("run_id", nargs="?", default="last", help="run id (default: the most recent)")
    p_trace.add_argument("--full", action="store_true", help="include every gate output line, not just failures")
    p_trace.add_argument("--json", action="store_true", help="print the raw NDJSON events instead of the timeline")
    p_trace.set_defaults(func=cmd_trace)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


def entry() -> None:
    """Console-script entry point (pyproject [project.scripts])."""
    sys.exit(main())


if __name__ == "__main__":
    entry()
