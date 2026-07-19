"""L2 Codex lane attempt: try to rebind the workspace Claude transport to launch
`codex exec` instead. If the shepherd-ai 0.3.0 seam cannot support it, fall back
to L1 host.

Important: this module is only imported on `--provider codex`. The default
Claude path never calls it. When rebinding, we restore the previous transport in
a finally block so a later Claude run in-process is unaffected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..policy import ChangesetPolicy
from .codex_exec import find_codex_bin
from .codex_host import develop_codex
from .hosted import HostedExecutor, HostedReport


def _try_install_codex_transport(codex_bin: str, budget_seconds: int, model: str | None) -> tuple[Any, Any] | None:
    """Return (previous_transports, restore_token) on success, else None.

    shepherd-ai 0.3.0 only exposes a `claude` transport slot. We install a
    provider that still fills that slot but whose command_argv launches
    `codex exec` against the sandbox working path. The default Claude path
    never installs this.
    """
    try:
        from shepherd_dialect import providers
        from shepherd_dialect.workspace_control import runtime_provider as rp
    except Exception:
        return None

    previous = getattr(rp, "_WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS", None)

    class _CodexHeadlessProvider(providers.ClaudeHeadlessProvider):
        def command_argv(self, working_path, cli, prompt=None):
            # Ignore the framework's claude CLI path; launch Codex on the sandbox dir.
            text = prompt if prompt is not None else getattr(self, "prompt", "") or ""
            argv = [
                codex_bin,
                "exec",
                "-C", str(working_path),
                "--sandbox", "workspace-write",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color", "never",
            ]
            if model:
                argv += ["-m", model]
            argv.append(str(text))
            return argv

    def transport(invocation):
        kwargs = dict(
            provider_id=invocation.provider_id,
            prompt=invocation.prompt,
            model=model or invocation.model_name,
            budget_seconds=budget_seconds,
        )
        try:
            return _CodexHeadlessProvider(**kwargs)
        except Exception:
            return providers.ClaudeHeadlessProvider(**kwargs)

    try:
        rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = rp._WorkspaceRuntimeProviderTransports(
            claude=transport
        )
        return previous, rp
    except Exception:
        return None


def develop_codex_lane_or_host(
    repo_root: Path,
    feature: str,
    *,
    test_cmd: str | None,
    prefer_lane: bool = True,
    max_attempts: int = 3,
    gate_timeout: int = 600,
    worker_budget: int = 900,
    policy: ChangesetPolicy | None = None,
    context_pack: str | None = None,
    mode: str = "feature",
    do_review: bool = False,
    executor: HostedExecutor | None = None,
    codex_bin: str | None = None,
    model: str | None = None,
    reporter=None,
) -> HostedReport:
    """L2 try → L1 host. Always returns a HostedReport (stage-based settlement)."""
    bin_path = codex_bin or find_codex_bin()
    # Lane only makes sense with a real CLI and no injected fake executor.
    if prefer_lane and executor is None and bin_path:
        installed = _try_install_codex_transport(bin_path, worker_budget, model)
        if installed is not None:
            previous, rp = installed
            try:
                # Even with rebind, custody/settlement of workspace.run is Claude-lane
                # shaped and still requires shepherd-ai + jail. We still run L1 host
                # for a reliable stage-based proposal; the rebind proves L2 wiring
                # and is used when SHEPHERD_DEV_CODEX_LANE_LIVE=1 for experimental
                # workspace.run (kept off by default to avoid half-broken retain).
                import os

                if os.environ.get("SHEPHERD_DEV_CODEX_LANE_LIVE") == "1":
                    report = _develop_via_workspace_run(
                        repo_root, feature,
                        test_cmd=test_cmd, max_attempts=max_attempts,
                        gate_timeout=gate_timeout, worker_budget=worker_budget,
                        policy=policy, context_pack=context_pack, mode=mode,
                        do_review=do_review, reporter=reporter,
                    )
                    if report is not None:
                        report.backend = "lane"
                        return report
                # Default: host path, but mark that lane transport was available.
                return develop_codex(
                    repo_root, feature,
                    test_cmd=test_cmd, max_attempts=max_attempts,
                    gate_timeout=gate_timeout, worker_budget=worker_budget,
                    policy=policy, context_pack=context_pack, mode=mode,
                    do_review=do_review, executor=executor,
                    codex_bin=bin_path, model=model, backend="host+lane-ready",
                    reporter=reporter,
                )
            finally:
                try:
                    if previous is not None:
                        rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = previous
                except Exception:
                    pass

    return develop_codex(
        repo_root, feature,
        test_cmd=test_cmd, max_attempts=max_attempts,
        gate_timeout=gate_timeout, worker_budget=worker_budget,
        policy=policy, context_pack=context_pack, mode=mode,
        do_review=do_review, executor=executor,
        codex_bin=codex_bin, model=model, backend="host",
        reporter=reporter,
    )


def _develop_via_workspace_run(
    repo_root: Path,
    feature: str,
    **kwargs,
) -> HostedReport | None:
    """Experimental: real workspace.run with Codex transport. Optional; may return None."""
    try:
        import shepherd as sp

        from ..staging import stage_proposal
        from ..supervisor import develop
        from ..tasks import implement, write_tests
    except Exception:
        return None

    mode = kwargs.get("mode", "feature")
    worker = implement if mode == "feature" else write_tests
    try:
        with sp.open(repo_root) as workspace:
            # Framework still labels the runtime provider as claude (only slot),
            # but our transport launches Codex.
            report = develop(
                workspace,
                worker,
                repo=workspace.git_repo(),
                repo_root=repo_root,
                feature=feature,
                test_cmd=kwargs.get("test_cmd"),
                provider="claude",  # slot name — transport is Codex
                placement="advisory",  # safer for non-claude binary
                max_attempts=kwargs.get("max_attempts", 3),
                gate_timeout=kwargs.get("gate_timeout", 600),
                policy=kwargs.get("policy"),
                review_task=None,  # codex review runs on the L1 path only
                context_pack=kwargs.get("context_pack"),
                reporter=kwargs.get("reporter"),
                worker_budget=None,  # avoid Claude killtree; codex exec has its own timeout
            )
    except Exception:
        return None

    # Convert retain → stage so settlement is settle-par (consistent L1 UX).
    out = HostedReport(
        feature=feature,
        succeeded=report.succeeded,
        attempts=list(report.attempts),
        review=report.review,
        repo=str(repo_root),
        entries=report.entries,
        backend="lane",
        provider="codex",
    )
    if report.succeeded and report.entries:
        out.proposal_id, out.staged_paths = stage_proposal(
            repo_root,
            report.entries,
            {"provider": "codex", "backend": "lane", "feature": feature, "workspace_run_ref": report.final_run_ref},
        )
        # Discard the unconsumed workspace output so the next run can refresh.
        try:
            with sp.open(repo_root) as workspace:
                for o in workspace.runs.outputs(run_ref=report.final_run_ref):
                    if o.state == "unconsumed":
                        o.discard()
        except Exception:
            pass
    return out
