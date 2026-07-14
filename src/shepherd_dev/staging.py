"""Staged proposal I/O — pure stdlib + materialize_into, NO shepherd-ai import.

Used by parallel workers, best-of, and the Grok host path (settle-par).
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .supervisor import materialize_into

PROPOSALS_DIR = ".shepherd-proposals"


def stage_proposal(
    repo_root: Path, entries: dict[str, bytes], manifest_extra: dict
) -> tuple[str, list[str]]:
    """Stage a proposal under .shepherd-proposals/<id>/."""
    proposal_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    staging = repo_root / PROPOSALS_DIR / proposal_id
    written = materialize_into(staging / "files", entries)
    manifest = {**manifest_extra, "paths": sorted(entries)}
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return proposal_id, written


_stage_proposal = stage_proposal
