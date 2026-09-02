"""Check the worker can authenticate BEFORE the run spends anything.

Two failures in the history cost a whole attempt each to discover, and the
attempt was the cheapest part: the pack had been built, the worktree
re-adopted, the workspace forked, the jailed CLI booted — and then `Not
logged in`, or `You've hit your weekly limit`. Eight attempts died on a
login the substrate had seeded from a blob that was already invalid; sixteen
on an allowance that was already spent.

The substrate refuses a blob whose `expiresAt` has passed. It cannot do more
from inside the jail: the CLI there sees a copy of the credential and a
redirected config dir, so a refresh it performs is written nowhere that
survives the launch. Refreshing has to happen out here, in the parent, with
the real config dir — which is exactly what one unjailed `claude -p ok`
does: the CLI refreshes a token that is expired or about to expire and
persists it where the next seeding reads it. The same call, read the other
way, is the cheapest possible allowance check: an exhausted quota answers
with the same message a worker would have died on fifteen minutes later.

So: resolve the credential; a missing one fails in zero seconds; a
subscription token that is expired or expires within a run's reach is
refreshed by a probe; a probe that reports no allowance or no login stops the
run before the worker exists; a probe that merely could not run (network,
timeout) is reported and the run proceeds — a flaky probe must not block work
the worker could still do.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

#: A worker attempt may run 15 minutes and the review as long again; a token
#: that expires inside that window fails the review, not the login.
AUTH_REFRESH_WINDOW_S = 45 * 60

#: The probe is one short model call; anything longer is the network or the
#: CLI, not the answer.
PROBE_TIMEOUT_S = 60

PROBE_PROMPT = "Reply with the single word: ok"

_OPT_OUT_VAR = "SHEPHERD_DEV_NO_AUTH_PREFLIGHT"

#: Texts the CLI answers with when the account, not the run, is the problem.
#: Each stops the run; a worker could not have done anything about them.
_NO_LOGIN = re.compile(
    r"not logged in|please run /login|oauth access token has been revoked|"
    r"authentication_error|invalid api key|401",
    re.IGNORECASE,
)


@dataclass
class PreflightResult:
    ok: bool
    #: what was done: `skipped`, `checked`, `refreshed`, `probed`, `failed`
    action: str
    detail: str = ""
    #: seconds until the (possibly refreshed) credential expires; None = unknown
    expires_in_s: float | None = None
    warnings: list[str] = field(default_factory=list)


def credential_expires_in(blob: bytes | str | None, now: float | None = None) -> float | None:
    """Seconds until the seeded subscription blob expires, from its
    `claudeAiOauth.expiresAt` (epoch milliseconds). None when the blob has no
    readable expiry — a shape this code does not know is not evidence either
    way, and the substrate's own check treats it the same."""
    if not blob:
        return None
    try:
        data = json.loads(blob.decode("utf-8") if isinstance(blob, bytes) else blob)
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        expires_at = oauth.get("expiresAt") if isinstance(oauth, dict) else None
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            return None
        return expires_at / 1000.0 - (time.time() if now is None else now)
    except Exception:
        return None


def quota_reason(text: str) -> str | None:
    """The supervisor's own reading of a spent allowance, applied to probe
    output instead of an exception."""
    from .supervisor import _QUOTA_MARKERS, _RESET_HINT

    lowered = (text or "").lower()
    if not any(marker in lowered for marker in _QUOTA_MARKERS):
        return None
    hint = _RESET_HINT.search(text or "")
    reason = "the API allowance is exhausted"
    return f"{reason} ({hint.group(0).strip()})" if hint else reason


def _default_resolve():
    """The substrate's credential resolution: (mode, blob, status). Never
    raises — a substrate that cannot be imported reads as unknown."""
    try:
        from shepherd_dialect.providers import _resolve_claude_auth_diagnostic

        resolution = _resolve_claude_auth_diagnostic()
        return resolution.mode, resolution.blob, getattr(resolution, "status", "") or ""
    except Exception as exc:  # pragma: no cover - substrate absent or changed
        return "unknown", None, f"could not resolve auth: {type(exc).__name__}"


def credential_sources(now: float | None = None) -> dict[str, float | None]:
    """Seconds-to-expiry of each host credential store the substrate might
    seed from, keyed by source: `file` (`$CLAUDE_CONFIG_DIR/.credentials.json`
    or `~/.claude/.credentials.json`) and, on macOS, `keychain`. A source that
    is absent or unreadable is simply not listed. Values are expiries only;
    the secrets never leave the store."""
    import subprocess as _sp
    import sys as _sys
    from pathlib import Path

    out: dict[str, float | None] = {}
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    candidates = []
    if config_dir:
        candidates.append(Path(config_dir) / ".credentials.json")
    candidates.append(Path.home() / ".claude" / ".credentials.json")
    for path in candidates:
        try:
            if path.is_file():
                out["file"] = credential_expires_in(path.read_bytes(), now=now)
                break
        except Exception:
            continue
    if _sys.platform == "darwin":
        try:
            proc = _sp.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, timeout=10, check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                out["keychain"] = credential_expires_in(proc.stdout.strip(), now=now)
        except Exception:
            pass
    return out


def stale_file_shadowing(sources: dict[str, float | None], window_s: int = 0) -> str | None:
    """The one shape the substrate's lookup order gets wrong, named: a
    credentials FILE that is expired (or inside the window) while the
    keychain holds a login that is not. The file is read first, so the dead
    token is what gets seeded — and the CLI, which uses the keychain, keeps
    working, which is why nothing else complains. Observed on a real
    machine: a file from a month earlier, 690 hours past expiry, in front of
    a keychain login good for another six."""
    file_exp = sources.get("file")
    key_exp = sources.get("keychain")
    if file_exp is None or key_exp is None:
        return None
    if file_exp < window_s <= key_exp:
        return (
            f"a stale ~/.claude/.credentials.json (expired {_fmt(-file_exp)} ago) shadows a live "
            f"keychain login (expires in {_fmt(key_exp)}): the substrate seeds the file first. "
            "Remove or refresh that file, or point CLAUDE_CONFIG_DIR at a directory holding a "
            "current copy"
        )
    return None


def _default_run_probe(timeout: int) -> tuple[bool, str]:
    """One unjailed `claude -p ok` with the REAL config dir. Returns (ran,
    text): `ran` False when the CLI could not be executed at all; `text` is
    stdout+stderr for classification."""
    cli = shutil.which("claude")
    if not cli:
        return False, "`claude` not found on PATH"
    try:
        proc = subprocess.run(
            [cli, "-p", PROBE_PROMPT, "--output-format", "json", "--max-turns", "1",
             "--strict-mcp-config", "--no-chrome"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, f"probe timed out after {timeout}s"
    except OSError as exc:
        return False, f"probe could not run: {exc}"
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        return True, text
    return True, text


def _probe_verdict(text: str) -> str | None:
    """Why the probe's answer means the run cannot proceed, or None."""
    quota = quota_reason(text)
    if quota:
        return f"worker unavailable: {quota}"
    if _NO_LOGIN.search(text or ""):
        return "the claude CLI is not logged in (run `claude login`, or set CLAUDE_CODE_OAUTH_TOKEN)"
    try:
        head = text.strip().splitlines()
        payload = json.loads(head[0]) if head and head[0].startswith("{") else None
    except Exception:
        payload = None
    if isinstance(payload, dict) and payload.get("is_error") is True:
        return f"the claude CLI answered with an error: {str(payload.get('result') or '')[:200]}"
    return None


def auth_preflight(
    *,
    probe: bool = False,
    window_s: int = AUTH_REFRESH_WINDOW_S,
    timeout: int = PROBE_TIMEOUT_S,
    resolve: Callable[[], tuple] | None = None,
    run_probe: Callable[[int], tuple[bool, str]] | None = None,
    environ=None,
    sources: Callable[[], dict] | None = None,
) -> PreflightResult:
    """Decide whether a claude-provider run may start, refreshing a
    subscription token that would not last it.

    `probe=True` runs the probe unconditionally (a repo's
    `preflight.auth_probe`), which is what turns "the allowance is spent"
    into a zero-cost refusal instead of a lost attempt. The default probes
    only when the token needs refreshing, so an ordinary run adds nothing.
    """
    env = os.environ if environ is None else environ
    if env.get(_OPT_OUT_VAR):
        return PreflightResult(True, "skipped", f"{_OPT_OUT_VAR} is set")
    resolve = resolve or _default_resolve
    run_probe = run_probe or _default_run_probe
    sources = sources or credential_sources

    mode, blob, status = resolve()
    if mode is None:
        return PreflightResult(
            False, "failed",
            f"no claude credential for the worker ({status or 'none found'}) — run `claude login` "
            "or set ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN",
        )

    expires_in = credential_expires_in(blob) if mode == "subscription_login" else None
    needs_refresh = mode == "subscription_login" and expires_in is not None and expires_in < window_s
    if not (probe or needs_refresh):
        return PreflightResult(True, "checked", _describe(mode, expires_in), expires_in_s=expires_in)

    ran, text = run_probe(timeout)
    if not ran:
        # The probe is a convenience; its absence is not evidence against
        # the credential. Say so and let the run try.
        result = PreflightResult(True, "probed", _describe(mode, expires_in), expires_in_s=expires_in)
        result.warnings.append(f"auth probe did not run ({text}); proceeding without it")
        if needs_refresh:
            result.warnings.append(
                f"the subscription token expires in {_fmt(expires_in)} and could not be refreshed here"
            )
        return result

    verdict = _probe_verdict(text)
    if verdict:
        return PreflightResult(False, "failed", verdict, expires_in_s=expires_in)

    # The probe ran with the real config dir; whatever it refreshed is now
    # what the substrate will seed. Read it back.
    mode2, blob2, _ = resolve()
    expires_after = credential_expires_in(blob2) if mode2 == "subscription_login" else None
    if needs_refresh:
        if expires_after is not None and (expires_in is None or expires_after > expires_in + 60):
            return PreflightResult(
                True, "refreshed",
                f"subscription token refreshed (expires in {_fmt(expires_after)})",
                expires_in_s=expires_after,
            )
        if expires_after is not None and expires_after <= 0:
            # Still expired after a probe that ran: the substrate refuses to
            # seed an expired blob before launch (its own preflight), so the
            # run is known-doomed — say so here, before the pack and the
            # adoption, and name the cause when it is the one we know: a
            # stale credentials file read ahead of a live keychain login.
            try:
                shadow = stale_file_shadowing(sources() or {})
            except Exception:
                shadow = None
            detail = shadow or (
                "the seeded `claude` subscription login is expired and the probe could not "
                "refresh the store the substrate reads from — run `claude login`, or set "
                "CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`)"
            )
            return PreflightResult(False, "failed", detail, expires_in_s=expires_after)
        result = PreflightResult(True, "probed", _describe(mode2, expires_after), expires_in_s=expires_after)
        result.warnings.append(
            f"the subscription token still expires in {_fmt(expires_after)}; a run longer than that "
            "will lose its review — run `claude login` if this repeats"
        )
        return result
    return PreflightResult(True, "probed", _describe(mode2, expires_after), expires_in_s=expires_after)


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return "the past"
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes / 60:.1f} h"


def _describe(mode: str | None, expires_in: float | None) -> str:
    if mode == "subscription_login":
        return f"subscription login (expires in {_fmt(expires_in)})"
    if mode == "api_key":
        return "ANTHROPIC_API_KEY"
    if mode == "oauth_token":
        return "CLAUDE_CODE_OAUTH_TOKEN"
    return str(mode or "unknown")
