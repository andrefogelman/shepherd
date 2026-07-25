"""Findings ledger: what the reviewer raised, and how each item actually ended.

A review verdict is a snapshot — issues appear in one run's prose and are gone
by the next. That is how a cycle ends with open problems while reading like
success: nothing carries the list forward, so nothing can notice it is still
non-empty. The ledger fixes the list in place. Every issue gets a stable id,
survives across rounds, and leaves only through a terminal state.

Severity is stripped before hashing on purpose. Re-labelling a finding from
HIGH to MEDIUM is not a fix, and a ledger keyed on the raw text would treat the
re-labelled text as a brand new problem while quietly closing the original.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

#: Findings leave the open set only through one of these.
TERMINAL_STATES = ("fixed", "blocked", "refused")
#: blocked/refused are human-or-agent judgements — they must say why.
_REASON_REQUIRED = ("blocked", "refused")

_SEVERITY = (
    r"blocker|blocking|bloqueante|critical|cr[ií]tic[oa]|severe|"
    r"high|alt[oa]|major|medium|m[eé]di[oa]|moderate|"
    r"low|baix[oa]|minor|nit|trivial|info"
)
#: A leading severity label: optional bracket, one or more severity words
#: chained by an arrow/slash (the "HIGH -> MEDIUM" downgrade), then a separator
#: strong enough to be punctuation rather than prose. A bare hyphen glued to
#: the word is NOT a separator, so "low-level cache" keeps its first word.
_SEVERITY_PREFIX = re.compile(
    rf"^[\s\[\(\*#>-]*(?:{_SEVERITY})"
    rf"(?:\s*(?:->|=>|→|/|,|\|)\s*(?:{_SEVERITY}))*"
    rf"(?:\s*[:\]\)\|]\s*|\s+[-–—]\s+|\s*[–—]\s*)",
    re.IGNORECASE,
)


def normalize_finding(text: str) -> str:
    """Identity of a finding: its problem statement, minus label and shape."""
    stripped = _SEVERITY_PREFIX.sub("", text or "", count=1)
    return re.sub(r"\s+", " ", stripped).strip().strip(".;:!").lower()


def finding_id(text: str) -> str:
    return hashlib.sha256(normalize_finding(text).encode("utf-8")).hexdigest()[:12]


@dataclass
class Finding:
    id: str
    text: str
    state: str = "open"
    reason: str | None = None
    #: every round whose review raised this — the trail a human reads to see
    #: whether an item was addressed once or kept coming back
    rounds: list[int] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "state": self.state,
            "reason": self.reason,
            "rounds": list(self.rounds),
        }


def _round_span(rounds: list[int]) -> str:
    if not rounds:
        return "unseen"
    if len(rounds) == 1:
        return f"round {rounds[0]}"
    return f"rounds {rounds[0]}-{rounds[-1]}"


class Ledger:
    """Ordered set of findings, keyed by normalized problem statement."""

    def __init__(self) -> None:
        self._by_id: dict[str, Finding] = {}

    @property
    def findings(self) -> list[Finding]:
        return list(self._by_id.values())

    def record_round(self, number: int, issues: list[str] | None) -> None:
        """Fold one review's issues in.

        Raised again: still open, trail grows. Raised before and absent now:
        the reviewer stopped objecting, so `fixed`. Already terminal: left
        alone — a later review cannot undo a judgement someone recorded.
        """
        seen: set[str] = set()
        for text in issues or []:
            fid = finding_id(text)
            seen.add(fid)
            existing = self._by_id.get(fid)
            if existing is None:
                self._by_id[fid] = Finding(id=fid, text=text, rounds=[number])
                continue
            if existing.state in _REASON_REQUIRED:
                continue  # blocked/refused stand; re-raising does not reopen
            existing.state = "open"
            if number not in existing.rounds:
                existing.rounds.append(number)
        for fid, finding in self._by_id.items():
            if fid not in seen and finding.state == "open" and finding.rounds:
                finding.state = "fixed"

    def close(self, finding_id_: str, state: str, reason: str | None = None) -> Finding:
        if state not in TERMINAL_STATES:
            raise ValueError(f"not a terminal state: {state!r} (use one of {TERMINAL_STATES})")
        if state in _REASON_REQUIRED and not (reason or "").strip():
            raise ValueError(f"{state} requires a reason")
        finding = self._by_id.get(finding_id_)
        if finding is None:
            raise KeyError(finding_id_)
        finding.state = state
        finding.reason = (reason or "").strip() or None
        return finding

    def open_findings(self) -> list[Finding]:
        return [f for f in self._by_id.values() if f.state == "open"]

    def has_open(self) -> bool:
        return any(f.state == "open" for f in self._by_id.values())

    def render(self) -> str:
        """The tally, printed whether or not anyone asked for it."""
        if not self._by_id:
            return ""
        lines = ["findings:"]
        for f in self._by_id.values():
            head = f"  [{f.state}] {f.text}  ({_round_span(f.rounds)})"
            lines.append(head if not f.reason else f"{head}\n    reason: {f.reason}")
        return "\n".join(lines)

    def guidance(self) -> str:
        """The open items, as the next round's marching orders."""
        openings = self.open_findings()
        if not openings:
            return ""
        items = "\n".join(f"- [{f.id}] {f.text}" for f in openings)
        return items

    def to_payload(self) -> list[dict]:
        return [f.to_payload() for f in self._by_id.values()]

    @classmethod
    def from_payload(cls, payload: list[dict] | None) -> "Ledger":
        led = cls()
        for row in payload or []:
            fid = row.get("id") or finding_id(row.get("text", ""))
            led._by_id[fid] = Finding(
                id=fid,
                text=row.get("text", ""),
                state=row.get("state", "open"),
                reason=row.get("reason"),
                rounds=list(row.get("rounds") or []),
            )
        return led
