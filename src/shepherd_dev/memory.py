"""Per-repo learned memory: curated facts from previous runs, injected into
the context pack so each worker starts knowing what earlier workers learned.

Curation rule (quality over quantity): only facts CONFIRMED by evidence enter —
gate gotchas from runs that eventually PASSED, and reviewer notes from APPROVED
reviews. Failed runs teach nothing here (unconfirmed). Capped, deduped, newest
wins. Speed (less exploration, fewer retries) and quality (known mistakes are
not repeated).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

MEMORY_DIR = Path(
    os.environ.get("SHEPHERD_DEV_MEMORY_DIR") or Path.home() / ".shepherd-dev" / "memory"
)
MAX_FACTS = 30
FACT_CHARS = 200
MEMORY_TEXT_CAP = 2_000

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _memory_path(repo_root: Path) -> Path:
    resolved = str(Path(repo_root).resolve())
    digest = hashlib.sha1(resolved.encode()).hexdigest()[:8]
    return MEMORY_DIR / f"{Path(resolved).name}-{digest}.json"


def load_facts(repo_root: Path) -> list[dict]:
    try:
        data = json.loads(_memory_path(repo_root).read_text(encoding="utf-8"))
        facts = data.get("facts", [])
        return facts if isinstance(facts, list) else []
    except Exception:
        return []


def memory_text(repo_root: Path, cap: int = MEMORY_TEXT_CAP) -> str:
    """Newest-first bullet list for the context pack. Empty string when none."""
    facts = load_facts(repo_root)
    lines: list[str] = []
    used = 0
    for fact in reversed(facts):  # newest last in storage -> newest first here
        text = str(fact.get("t", "")).strip()
        if not text:
            continue
        line = f"- {text}"
        if used + len(line) + 1 > cap:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def sanitize_fact(text: str) -> str:
    """One line of plain text, safe to paste into the context pack as a bullet.

    A fact is not authored by us. It comes from a test suite's stdout or a
    reviewer's prose, and it is injected into EVERY later pack for this repo —
    so a repository can leave text of its choosing in front of every future
    worker. Two things follow.

    Newlines are collapsed. The pack is a flat document whose structure is line
    based (`== FILE: …`, `== REPO MEMORY …`), so a fact containing a newline
    could forge a section header and dress attacker text as pack structure.
    _first_error_line already yields one line; reviewer issues do not.

    Control characters go too — a stray ESC re-enters the ANSI handling the
    regex above just stripped, and a NUL truncates in whatever reads it next.

    What this does NOT do is make the content trustworthy. It makes it
    unambiguously one bullet of data; the pack labels it as learned notes, and
    the worker is told to treat the pack as reference rather than instruction.
    """
    flat = _ANSI.sub("", text)
    flat = "".join(" " if ch in "\r\n\t" else ch for ch in flat if ch.isprintable() or ch in "\r\n\t ")
    return " ".join(flat.split())[:FACT_CHARS]


def add_facts(repo_root: Path, texts: list[str], source: str | None) -> int:
    """Append curated facts (dedup by normalized text; cap MAX_FACTS, newest kept).

    Read-modify-write under an exclusive lock, then an atomic replace. runN,
    run2 and best-of all reach this from concurrent lanes: unlocked, two lanes
    read the same list and the later write dropped the earlier lane's facts,
    and a write interrupted partway left JSON that load_facts reads as an empty
    memory — losing every fact the repo had learned, silently, because the
    except returns [].
    """
    cleaned = [t for t in (sanitize_fact(text) for text in texts) if t]
    if not cleaned:
        return 0
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return 0

    path = _memory_path(repo_root)
    lock_path = path.with_suffix(".lock")
    added = 0
    try:
        with open(lock_path, "a+") as lock:
            try:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass  # no flock (non-POSIX): best-effort, as history.py does

            # Read INSIDE the lock: reading outside it is what makes the write
            # a lost update rather than an append.
            facts = load_facts(repo_root)
            known = {f.get("t", "").lower() for f in facts}
            for t in cleaned:
                if t.lower() in known:
                    continue
                facts.append({"t": t, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "src": source})
                known.add(t.lower())
                added += 1
            facts = facts[-MAX_FACTS:]

            # Atomic: a reader sees the old file or the new one, never a
            # half-written one that parses as no memory at all.
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps({"facts": facts}, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, path)
    except Exception:
        return 0
    return added


def _first_error_line(tail: str) -> str:
    for line in _ANSI.sub("", tail).splitlines():
        line = line.strip()
        if line and not line.startswith(("ℹ", "#", "TAP", "ok ")):
            return line[:FACT_CHARS]
    return ""


def learn_from_report(repo_root: Path, report) -> int:
    """Extract confirmed facts from a DevReport. Only successful runs teach."""
    if not getattr(report, "succeeded", False):
        return 0
    facts: list[str] = []
    attempts = getattr(report, "attempts", [])
    for attempt in attempts[:-1]:  # failures BEFORE the eventual pass = confirmed gotchas
        if attempt.verdict == "tests_failed" and attempt.gate and attempt.gate.output_tail:
            line = _first_error_line(attempt.gate.output_tail)
            if line:
                facts.append(f"gate gotcha (fixed after retry): {line}")
    review = getattr(report, "review", None)
    if review is not None and getattr(review, "approved", False) and not getattr(review, "error", None):
        for issue in (review.issues or [])[:3]:
            facts.append(f"reviewer note: {issue}")
    return add_facts(repo_root, facts, getattr(report, "final_run_ref", None))


def learn_from_review(repo_root: Path, review, source: str | None = None) -> int:
    """Curate facts from a standalone (combined / winner) review verdict."""
    if review is None or not getattr(review, "approved", False) or getattr(review, "error", None):
        return 0
    facts = [f"reviewer note: {issue}" for issue in (review.issues or [])[:3]]
    return add_facts(repo_root, facts, source)
