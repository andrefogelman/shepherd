"""L2 Grok lane attempt: try to rebind the workspace Claude transport to launch
Grok instead. If the shepherd-ai 0.3.0 seam cannot support it, fall back to L1 host.

Important: this module is only imported on `--provider grok`. The default Claude
path never calls `try_lane_or_host`. When rebinding, we restore the previous
transport in a finally block so a later Claude run in-process is unaffected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..policy import ChangesetPolicy
from .grok_exec import GrokExecutor, find_grok_bin
from .grok_host import GrokHostReport, develop_grok


def _try_install_grok_transport(grok_bin: str, budget_seconds: int, model: str | None) -> tuple[Any, Any] | None:
    """Return (previous_transports, restore_token) on success, else None.

    shepherd-ai 0.3.0 only exposes a `claude` transport slot. We install a
    provider that still fills that slot but whose command_argv launches Grok
    against the sandbox working path. Claude default path never installs this.
    """
    try:
        from shepherd_dialect import providers
        from shepherd_dialect.workspace_control import runtime_provider as rp
    except Exception:
        return None

    previous = getattr(rp, "_WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS", None)

    class _GrokHeadlessProvider(providers.ClaudeHeadlessProvider):
        def command_argv(self, working_path, cli, prompt=None):
            # Ignore the framework's claude CLI path; launch Grok on the sandbox dir.
            text = prompt if prompt is not None else getattr(self, "prompt", "") or ""
            argv = [
                grok_bin,
                "--cwd", str(working_path),
                "--always-approve",
                "--permission-mode", "bypassPermissions",
                "--max-turns", "40",
                "--no-memory",
                "--output-format", "plain",
            ]
            if model:
                argv += ["--model", model]
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
            return _GrokHeadlessProvider(**kwargs)
        except Exception:
            return providers.ClaudeHeadlessProvider(**kwargs)

    try:
        rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = rp._WorkspaceRuntimeProviderTransports(
            claude=transport
        )
        return previous, rp
    except Exception:
        return None


def develop_grok_lane_or_host(
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
    executor: GrokExecutor | None = None,
    grok_bin: str | None = None,
    model: str | None = None,
    reporter=None,
) -> GrokHostReport:
    """L2 try → L1 host. Always returns a GrokHostReport (stage-based settlement)."""
    bin_path = grok_bin or find_grok_bin()
    # Lane only makes sense with a real CLI and no injected fake executor.
    if prefer_lane and executor is None and bin_path:
        installed = _try_install_grok_transport(bin_path, worker_budget, model)
        if installed is not None:
            previous, rp = installed
            try:
                # Even with rebind, custody/settlement of workspace.run is Claude-lane
                # shaped and still requires shepherd-ai + jail. We still run L1 host
                # for a reliable stage-based proposal; the rebind proves L2 wiring
                # and is used when SHEPHERD_DEV_GROK_LANE_LIVE=1 for experimental
                # workspace.run (kept off by default to avoid half-broken retain).
                import os

                if os.environ.get("SHEPHERD_DEV_GROK_LANE_LIVE") == "1":
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
                report = develop_grok(
                    repo_root, feature,
                    test_cmd=test_cmd, max_attempts=max_attempts,
                    gate_timeout=gate_timeout, worker_budget=worker_budget,
                    policy=policy, context_pack=context_pack, mode=mode,
                    do_review=do_review, executor=executor,
                    grok_bin=bin_path, model=model, backend="host+lane-ready",
                    reporter=reporter,
                )
                return report
            finally:
                try:
                    if previous is not None:
                        rp._WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = previous
                except Exception:
                    pass

    return develop_grok(
        repo_root, feature,
        test_cmd=test_cmd, max_attempts=max_attempts,
        gate_timeout=gate_timeout, worker_budget=worker_budget,
        policy=policy, context_pack=context_pack, mode=mode,
        do_review=do_review, executor=executor,
        grok_bin=grok_bin, model=model, backend="host",
        reporter=reporter,
    )


def _develop_via_workspace_run(
    repo_root: Path,
    feature: str,
    **kwargs,
) -> GrokHostReport | None:
    """Experimental: real workspace.run with Grok transport. Optional; may return None."""
    try:
        import shepherd as sp

        from ..supervisor import develop, read_changeset_entries
        from ..tasks import implement, write_tests, review as review_task
        from ..staging import stage_proposal
    except Exception:
        return None

    mode = kwargs.get("mode", "feature")
    worker = implement if mode == "feature" else write_tests
    do_review = kwargs.get("do_review", False)
    try:
        with sp.open(repo_root) as workspace:
            # Framework still labels the runtime provider as claude (only slot),
            # but our transport launches Grok.
            report = develop(
                workspace,
                worker,
                repo=workspace.git_repo(),
                repo_root=repo_root,
                feature=feature,
                test_cmd=kwargs.get("test_cmd"),
                provider="claude",  # slot name — transport is Grok
                placement="advisory",  # safer for non-claude binary
                max_attempts=kwargs.get("max_attempts", 3),
                gate_timeout=kwargs.get("gate_timeout", 600),
                policy=kwargs.get("policy"),
                review_task=review_task if do_review else None,
                context_pack=kwargs.get("context_pack"),
                reporter=kwargs.get("reporter"),
                worker_budget=None,  # avoid Claude killtree; Grok has its own timeout
            )
    except Exception:
        return None

    # Convert retain → stage so settlement is settle-par (consistent L1 UX).
    out = GrokHostReport(
        feature=feature,
        succeeded=report.succeeded,
        attempts=list(report.attempts),
        review=report.review,
        repo=str(repo_root),
        entries=report.entries,
        backend="lane",
    )
    if report.succeeded and report.entries:
        out.proposal_id, out.staged_paths = stage_proposal(
            repo_root,
            report.entries,
            {"provider": "grok", "backend": "lane", "feature": feature, "workspace_run_ref": report.final_run_ref},
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
