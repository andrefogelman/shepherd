"""L1 Codex host worker: isolate → execute Codex → policy → gate → review → stage.

Delegates the supervised loop to `hosted.develop_hosted`. Unlike the Grok path
(heuristic review only), Codex ships a real LLM review: `codex exec` re-reads
the modified clone in a read-only sandbox and returns a structured verdict, so
`--auto-settle` is meaningful on this provider. Does NOT import or call
Claude / shepherd-ai. Settlement uses `.shepherd-proposals/` (`settle-par`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from ..policy import ChangesetPolicy
from ..supervisor import ReviewVerdict
from .codex_exec import build_executor, find_codex_bin
from .hosted import HostedExecutor, HostedReport, develop_hosted

# Injectable process runner for tests: (argv, timeout, last_message_path) → (returncode, output_tail)
ReviewRunner = Callable[..., tuple[int, str]]

REVIEW_BUDGET_DEFAULT = 300
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class CodexHostReport(HostedReport):
    """Codex-flavoured HostedReport (provider field pre-set by develop_codex)."""


def _default_runner(argv: list[str], *, timeout: int, last_message_path: str) -> tuple[int, str]:
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "CI": os.environ.get("CI", "1")},
    )
    tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-4000:]
    return proc.returncode, tail


def _review_prompt(entries: dict[str, bytes], feature: str) -> str:
    files = "\n".join(f"- {p}" for p in sorted(entries))
    return (
        "You are a skeptical code reviewer. The working directory contains a "
        "repository clone where the following files were just created or "
        f"modified to implement this feature request:\n\n{feature}\n\n"
        f"Changed files:\n{files}\n\n"
        "Read the changed files (and enough surrounding code to judge them). "
        "Look for: incomplete implementations, TODO/placeholder code, broken "
        "imports, style inconsistent with the codebase, missing error handling, "
        "and changes unrelated to the request.\n\n"
        "Answer with ONLY a JSON object, no prose, in exactly this shape:\n"
        '{"approved": true|false, "summary": "<one line>", "issues": ["<issue>", ...]}'
    )


def codex_review(
    clone: Path,
    entries: dict[str, bytes],
    feature: str,
    *,
    codex_bin: str | None,
    model: str | None = None,
    budget_seconds: int = REVIEW_BUDGET_DEFAULT,
    runner: ReviewRunner | None = None,
) -> ReviewVerdict:
    """LLM review of the modified clone via `codex exec` (read-only sandbox).

    Any failure degrades to a ReviewVerdict with `error` set — never raises, and
    an errored review never counts as approval.
    """
    if not codex_bin:
        return ReviewVerdict(
            False, "codex review unavailable", [],
            error="codex CLI not found — install @openai/codex or set SHEPHERD_DEV_CODEX_CMD",
        )
    run = runner or _default_runner
    with tempfile.NamedTemporaryFile(mode="r", suffix=".txt", delete=False) as tmp:
        last_message_path = tmp.name
    argv = [
        codex_bin,
        "exec",
        "-C", str(clone),
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color", "never",
        "-o", last_message_path,
    ]
    if model:
        argv += ["-m", model]
    argv.append(_review_prompt(entries, feature))
    try:
        code, tail = run(argv, timeout=max(60, budget_seconds), last_message_path=last_message_path)
    except subprocess.TimeoutExpired:
        return ReviewVerdict(False, "codex review timed out", [], error=f"timeout after {budget_seconds}s")
    except OSError as exc:
        return ReviewVerdict(False, "codex review could not launch", [], error=str(exc))
    finally:
        cleanup_path = Path(last_message_path)
    if code != 0:
        cleanup_path.unlink(missing_ok=True)
        return ReviewVerdict(False, "codex review failed", [], error=f"codex exited {code}: {tail[-300:]}")
    try:
        raw = cleanup_path.read_text()
    except OSError as exc:
        return ReviewVerdict(False, "codex review output unreadable", [], error=str(exc))
    finally:
        cleanup_path.unlink(missing_ok=True)
    verdict = _parse_verdict(raw)
    if verdict is None:
        return ReviewVerdict(
            False, "codex review verdict unparseable", [], error=f"no JSON verdict in: {raw[-300:]}"
        )
    return verdict


def _parse_verdict(raw: str) -> ReviewVerdict | None:
    """Extract {"approved":…} from the reviewer's final message (strict, then embedded)."""
    for candidate in (raw.strip(), *(m.group(0) for m in [_JSON_BLOCK.search(raw)] if m)):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "approved" in data:
            issues = data.get("issues") or []
            return ReviewVerdict(
                approved=bool(data["approved"]),
                summary=str(data.get("summary", ""))[:500],
                issues=[str(i)[:300] for i in issues if str(i).strip()][:20],
            )
    return None


def develop_codex(
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
    review_fn=None,
    executor: HostedExecutor | None = None,
    codex_bin: str | None = None,
    model: str | None = None,
    backend: str = "host",
    reporter=None,
) -> HostedReport:
    """Supervised Codex loop (L1 host). Never mutates repo_root; stages on success."""
    bin_path = codex_bin or find_codex_bin()
    execu = executor or build_executor(codex_bin=bin_path, model=model)
    if do_review and review_fn is None and executor is None and bin_path:
        # Real LLM review only when a live CLI drives the worker; with an
        # injected/fake executor there is no reason to assume codex works.
        def _live_review(clone, entries, feat):
            return codex_review(clone, entries, feat, codex_bin=bin_path, model=model)

        review_fn = _live_review
    return develop_hosted(
        repo_root,
        feature,
        provider="codex",
        executor=execu,
        test_cmd=test_cmd,
        max_attempts=max_attempts,
        gate_timeout=gate_timeout,
        worker_budget=worker_budget,
        policy=policy,
        context_pack=context_pack,
        mode=mode,
        do_review=do_review,
        review_fn=review_fn,
        backend=backend,
        reporter=reporter,
    )
