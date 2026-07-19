"""L1 Grok host worker: isolate → execute Grok → policy → gate → stage.

Delegates the supervised loop to `hosted.develop_hosted` (shared with the
Codex provider). Does NOT import or call Claude / shepherd-ai. Settlement uses
the same `.shepherd-proposals/` stage as run2/best-of (`settle-par`).
"""

from __future__ import annotations

from pathlib import Path

from ..policy import ChangesetPolicy
from ..supervisor import ReviewVerdict
from .grok_exec import GrokExecutor, build_executor
from .hosted import HostedReport, develop_hosted, heuristic_review

# Back-compat public names: callers/tests keep importing these from grok_host.
GrokHostReport = HostedReport
_heuristic_review = heuristic_review


def develop_grok(
    repo_root: Path,
    feature: str,
    *,
    test_cmd: str | None,
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
    backend: str = "host",
    reporter=None,
) -> HostedReport:
    """Supervised Grok loop (L1 host). Never mutates repo_root; stages on success."""
    execu = executor or build_executor(grok_bin=grok_bin, model=model)
    return develop_hosted(
        repo_root,
        feature,
        provider="grok",
        executor=execu,
        test_cmd=test_cmd,
        max_attempts=max_attempts,
        gate_timeout=gate_timeout,
        worker_budget=worker_budget,
        policy=policy,
        context_pack=context_pack,
        mode=mode,
        do_review=do_review,
        review_fn=None,  # grok has no LLM reviewer CLI: heuristic only
        backend=backend,
        reporter=reporter,
    )


__all__ = ["GrokHostReport", "develop_grok", "ReviewVerdict", "_heuristic_review"]
