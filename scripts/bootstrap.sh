#!/bin/bash
set -euo pipefail
# shepherd-dev bootstrap — installs the CLI as a uv/pipx tool if missing and
# checks the machine's runtime requirements. Idempotent; safe to re-run.

REPO_SSH="git+ssh://git@github.com/andrefogelman/shepherd.git"
REPO_HTTPS="git+https://github.com/andrefogelman/shepherd.git"

say() { printf '%s\n' "$*"; }

# ── CLI ──────────────────────────────────────────────────────────────────────
if command -v shepherd-dev >/dev/null 2>&1; then
  say "✅ shepherd-dev: $(command -v shepherd-dev)"
else
  if command -v uv >/dev/null 2>&1; then
    say "→ installing shepherd-dev via uv tool install..."
    uv tool install "$REPO_SSH" 2>/dev/null || uv tool install "$REPO_HTTPS"
  elif command -v pipx >/dev/null 2>&1; then
    say "→ installing shepherd-dev via pipx..."
    pipx install "$REPO_SSH" 2>/dev/null || pipx install "$REPO_HTTPS"
  else
    say "🛑 need uv (https://docs.astral.sh/uv/) or pipx to install shepherd-dev"
    exit 1
  fi
  # uv/pipx put shims in ~/.local/bin — pick them up for this check
  export PATH="$HOME/.local/bin:$PATH"
  command -v shepherd-dev >/dev/null 2>&1 && say "✅ shepherd-dev installed: $(command -v shepherd-dev)" || {
    say "🛑 install finished but shepherd-dev is not on PATH — add ~/.local/bin to PATH"
    exit 1
  }
fi

# ── runtime requirements ─────────────────────────────────────────────────────
if command -v claude >/dev/null 2>&1; then
  say "✅ claude CLI: $(command -v claude)"
else
  say "⚠️  claude CLI not found — the claude worker provider will not run (static provider still works)"
fi

PYV=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "none")
say "✅ python3: $PYV (needs >= 3.11)"

case "$(uname -s)" in
  Darwin) say "✅ sandbox: macOS Seatbelt" ;;
  Linux)
    KV=$(uname -r)
    say "✅ sandbox: Linux Landlock (kernel $KV; needs >= 5.13)" ;;
  *) say "⚠️  unsupported OS for the native jail — use WSL on Windows" ;;
esac

say ""
say "Per-repo setup (once): shepherd-dev init --repo <repo>"
say "  and gitignore: .vcscore/  REVIEW.json  .shepherd-proposals/"
