"""Staged proposal I/O — pure stdlib + materialize_into, NO shepherd-ai import.

Used by parallel workers, best-of, and the Grok host path (settle-par).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from .supervisor import materialize_into

PROPOSALS_DIR = ".shepherd-proposals"

#: Exactly what stage_proposal emits: YYYYmmdd-HHMMSS-<6 lowercase hex>.
#:
#: A proposal id names a directory, and settle built its path by joining the id
#: onto the repo root. Unvalidated, `../tests` named the repo's own test
#: directory and `../../outside` named one outside the repo — both then passed
#: to shutil.rmtree, since the only check was that the target held a `files/`
#: subdirectory. On the accept path the same traversal read a planted
#: manifest.json and handed its regate_cmd to a shell.
#:
#: An allow-list, not a denylist: refusing `..` would still admit absolute
#: paths, symlink-shaped ids, and the unicode lookalikes a denylist forgets.
#: Nothing but a generated id is a proposal id.
_PROPOSAL_ID_RE = re.compile(r"\A[0-9]{8}-[0-9]{6}-[0-9a-f]{6}\Z")


def is_proposal_id(candidate: str) -> bool:
    """True only for a string stage_proposal could have produced."""
    return bool(_PROPOSAL_ID_RE.match(candidate or ""))


def stage_proposal(
    repo_root: Path, entries: dict[str, bytes], manifest_extra: dict
) -> tuple[str, list[str]]:
    """Stage a proposal under .shepherd-proposals/<id>/."""
    proposal_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    assert is_proposal_id(proposal_id), proposal_id  # generator and check agree
    staging = repo_root / PROPOSALS_DIR / proposal_id
    written = materialize_into(staging / "files", entries)
    manifest = {**manifest_extra, "paths": sorted(entries)}
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return proposal_id, written


_stage_proposal = stage_proposal
