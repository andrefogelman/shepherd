"""Findings ledger: what the reviewer raised, and how each item actually ended.

A review verdict is a snapshot — issues appear in one run's prose and are gone
by the next. That is how a cycle ends with open problems while reading like
success: nothing carries the list forward, so nothing can notice it is still
non-empty. The ledger fixes the list in place. Every issue gets a stable id,
survives across rounds, and leaves only through a terminal state.

Severity is stripped before hashing on purpose. Re-labelling a finding from
HIGH to MEDIUM is not a fix, and a ledger keyed on the raw text would treat the
re-labelled text as a brand new problem while quietly closing the original.

Rewording is the same hazard one step up, and text normalization cannot reach
it: two sentences describing one problem in different words are different
sentences, and matching them by similarity would eventually fold two genuinely
different problems into one and close a real finding in silence — strictly
worse than the failure it repairs. So identity is not guessed. The reviewer is
handed the open findings with their ids and answers about them directly: an
item it still sees is re-raised under its id, an item it has verified as gone
goes in `resolved`. Absence closes nothing on a rejection, because a rejecting
reviewer that stopped mentioning something has told us nothing about it.

Approval closes the rest, but as `accepted`, never `fixed`: it is a judgement
over the change as a whole, not evidence that each remaining item was dealt
with. Recording those as `fixed` made the ledger claim work nobody did —
findings read `[fixed]` while still sitting in the delivered file.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

#: Findings leave the open set only through one of these.
#:
#: `fixed` is a claim about the CODE: this problem is gone. Only a reviewer
#: naming the finding in `resolved` earns it — that is the one signal that
#: says someone checked this item and found it addressed.
#:
#: `accepted` is a claim about the JUDGEMENT: the reviewer approved the change
#: as a whole while this was still open. It closes the item without asserting
#: the problem was solved, because approval does not say that. Stamping these
#: `fixed` made the ledger report work that was never done — findings showed
#: `[fixed]` while still sitting in the delivered file.
TERMINAL_STATES = ("fixed", "accepted", "blocked", "refused")
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


#: The labels that mean "I would not hold the change back for this". Everything
#: else a reviewer writes as a label — blocker, critical, high, major, medium —
#: holds it back, and so does no label at all: silence about severity is not
#: a downgrade.
_ADVISORY_WORDS = re.compile(
    r"^[\s\[\(\*#>-]*(?:low|baix[oa]|minor|nit|trivial|info|non-?blocking|n[aã]o[- ]bloqueante"
    r"|optional|opcional|suggestion|sugest[aã]o)\b",
    re.IGNORECASE,
)


def severity_of(text: str) -> str:
    """`advisory` when a plain-text finding labels itself as one, else
    `blocking`. Reads only a LEADING label — a sentence that merely contains
    the word "minor" somewhere is not labelling itself."""
    head = split_finding_id(text)[1]
    return "advisory" if _ADVISORY_WORDS.match(head or "") else "blocking"


#: How the reviewer points at an existing finding instead of restating it:
#: a bracketed id at the very start of the issue text.
_ID_PREFIX = re.compile(r"^\s*[\[(]([0-9a-f]{12})[\])]\s*")


def split_finding_id(text: str) -> tuple[str | None, str]:
    """Separate a leading `[id]` reference from the issue text behind it.

    The id is only a claim at this point — the caller checks it against the
    ledger, because a model can produce twelve plausible hex characters that
    match nothing. An unrecognised one falls back to the text identity, so a
    hallucinated reference never mints a finding keyed on the hallucination.
    """
    match = _ID_PREFIX.match(text or "")
    if match is None:
        return None, text or ""
    return match.group(1), (text or "")[match.end() :]


def normalize_finding(text: str) -> str:
    """Identity of a finding: its problem statement, minus label and shape."""
    stripped = _SEVERITY_PREFIX.sub("", split_finding_id(text)[1], count=1)
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

    def record_round(
        self,
        number: int,
        issues: list[str] | None,
        resolved: list[str] | None = None,
        approved: bool = False,
    ) -> None:
        """Fold one review's issues in.

        Raised again — by id, or by matching text: still open, trail grows.
        Named in `resolved`: the reviewer checked it and the problem is gone,
        so `fixed`. Merely absent from a rejection: still open, because silence
        is not a verdict. `approved` closes what is left, that being the one
        judgement that covers the change as a whole. Already terminal: left
        alone — a later review cannot undo a judgement someone recorded.

        A finding named in both `issues` and `resolved` stays open. The verdict
        contradicts itself and only one of the two readings can hide a real
        problem.
        """
        seen: set[str] = set()
        for text in issues or []:
            ref, _ = split_finding_id(text)
            fid = ref if ref in self._by_id else finding_id(text)
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
        for fid in resolved or []:
            finding = self._by_id.get(fid)
            if finding is None or fid in seen:
                continue  # unknown id, or contradicted by a re-raise
            if finding.state == "open" and finding.rounds:
                finding.state = "fixed"
        if approved:
            for finding in self._by_id.values():
                if finding.state == "open" and finding.rounds:
                    # `accepted`, NOT `fixed`. Approval is a judgement over the
                    # change as a whole; it is not evidence that this
                    # particular finding was addressed. The reviewer may have
                    # judged it tolerable, or simply not re-checked it. Only an
                    # explicit `resolved` says the problem is gone.
                    finding.state = "accepted"

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
