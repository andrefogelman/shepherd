"""Update notice: tell the user a newer shepherd-dev exists — without ever
touching the critical path.

Two decoupled halves, both failure-silent:

- ``update_notice(current)`` reads a LOCAL cache file and, when it records a
  newer version, returns a one-line hint (printed to stderr at the end of a
  command). Never does network I/O — a command's latency is untouched.
- ``maybe_refresh_in_background()`` refreshes that cache in a daemon thread
  when it is older than the TTL, by reading the published ``pyproject.toml``
  version from the repository. The result serves the NEXT invocation.

Opt out entirely with ``SHEPHERD_DEV_NO_UPDATE_CHECK=1``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

REMOTE_PYPROJECT = (
    "https://raw.githubusercontent.com/andrefogelman/shepherd/main/pyproject.toml"
)
CACHE_FILE = Path.home() / ".shepherd-dev" / "update-check.json"
TTL_SECONDS = 24 * 3600
FETCH_TIMEOUT = 4

UPDATE_CMD = "uv tool install --force git+https://github.com/andrefogelman/shepherd.git"

_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def _opted_out() -> bool:
    return bool(os.environ.get("SHEPHERD_DEV_NO_UPDATE_CHECK"))


def _parse(version: str) -> tuple[int, ...] | None:
    try:
        parts = tuple(int(p) for p in version.strip().split("."))
        return parts if parts else None
    except Exception:
        return None


def _is_newer(candidate: str, *, than: str) -> bool:
    a, b = _parse(candidate), _parse(than)
    return a is not None and b is not None and a > b


def _read_cache() -> dict | None:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _http_fetch(url: str, timeout: int) -> str:
    from urllib.request import Request, urlopen

    req = Request(url, headers={"User-Agent": "shepherd-dev-update-check"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https URL
        return resp.read().decode("utf-8", errors="replace")


def _refresh_cache(fetch=_http_fetch) -> None:
    """Fetch the published version and write the cache. Silent on ANY failure."""
    try:
        text = fetch(REMOTE_PYPROJECT, FETCH_TIMEOUT)
        match = _VERSION_RE.search(text)
        if not match:
            return
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps({"latest": match.group(1), "ts": time.time()}), encoding="utf-8"
        )
    except Exception:
        pass


def maybe_refresh_in_background(fetch=_http_fetch) -> threading.Thread | None:
    """Kick a daemon-thread cache refresh when the cache is stale. Returns the
    thread (tests join it), or None when fresh/opted out."""
    if _opted_out():
        return None
    cache = _read_cache()
    if cache is not None and time.time() - float(cache.get("ts", 0)) < TTL_SECONDS:
        return None
    thread = threading.Thread(
        target=_refresh_cache, kwargs={"fetch": fetch}, daemon=True,
        name="shepherd-update-check",
    )
    thread.start()
    return thread


def update_notice(current: str) -> str | None:
    """A one-line update hint when the cached latest version is newer than
    ``current`` — from the cache only, zero network, never raises."""
    if _opted_out():
        return None
    cache = _read_cache()
    if not cache:
        return None
    latest = str(cache.get("latest", ""))
    if not _is_newer(latest, than=current):
        return None
    return (
        f"update available: shepherd-dev {latest} (you have {current}) — "
        f"upgrade with: {UPDATE_CMD}"
    )
