"""Worker provider adapters (Claude path stays in supervisor/cli; Grok is opt-in)."""

from __future__ import annotations

from .grok_host import GrokHostReport, develop_grok

__all__ = ["GrokHostReport", "develop_grok"]
