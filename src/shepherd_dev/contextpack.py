"""Context pack: deterministic, zero-token context optimization for workers.

The single biggest worker cost is blind repo exploration (Read after Read).
Instead of intercepting API traffic (a proxy would conflict with the user's
main-session tooling), we pre-compute a compact context locally — file tree,
feature-relevant files (whole when small, signature skeletons when large) and
the repo's learned memory — and inject it into the worker/reviewer prompt.
Built ONCE per command and reused across retries / best-of candidates: the
honest analogue, in this lane, of the paper's prefix reuse.

Everything here is pure stdlib and deterministic: same repo state + same
feature => byte-identical pack.
"""

from __future__ import annotations

import re
from pathlib import Path

from .supervisor import IGNORED_DIRS

# Build artifacts poison relevance (minified bundles repeat every keyword);
# exclude them from the pack scan on top of the shared ignore set.
PACK_IGNORED_DIRS = IGNORED_DIRS | {
    "dist", "build", "out", ".next", "target", "coverage", "vendor", "public",
}

PACK_BUDGET = 25_000          # chars for the whole pack
TREE_LIMIT = 300              # max entries in the tree section
FULL_FILE_LIMIT = 3_500       # files up to this size go in whole
SKELETON_LINE_LIMIT = 60      # max signature lines per skeleton
SCAN_FILE_CAP = 4_000         # max files scored per repo
READ_CAP = 100_000            # bytes read per file for scoring/skeleton
MAX_FILE_BYTES = 400_000      # bigger than this: listed in tree only

_STOPWORDS = {
    # pt
    "com", "para", "que", "uma", "um", "de", "da", "do", "das", "dos", "em",
    "no", "na", "nos", "nas", "por", "criar", "crie", "adicionar", "adicione",
    "novo", "nova", "fazer", "usando", "sem", "mais", "como", "ser", "deve",
    "arquivo", "arquivos", "quando", "todos", "toda", "pelo", "pela",
    # en
    "the", "and", "for", "with", "that", "this", "add", "create", "new",
    "make", "use", "using", "should", "must", "file", "files", "when", "all",
    "implement", "feature", "function", "support",
}

_SKELETON_PREFIXES: dict[str, tuple[str, ...]] = {
    ".py": ("class ", "def ", "async def ", "from ", "import ", "@"),
    ".ts": ("import ", "export ", "const ", "function ", "class ", "type ", "interface ", "enum "),
    ".tsx": ("import ", "export ", "const ", "function ", "class ", "type ", "interface "),
    ".js": ("import ", "export ", "const ", "function ", "class ", "module.exports"),
    ".jsx": ("import ", "export ", "const ", "function ", "class "),
    ".mjs": ("import ", "export ", "const ", "function ", "class "),
    ".ex": ("defmodule ", "def ", "defp ", "defmacro ", "use ", "alias ", "import ", "@spec"),
    ".exs": ("defmodule ", "def ", "defp ", "use ", "alias ", "import "),
    ".go": ("package ", "import ", "func ", "type ", "var ", "const "),
    ".rs": ("pub ", "fn ", "struct ", "enum ", "impl ", "trait ", "use ", "mod "),
}

_TEXT_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".md",
    ".ex", ".exs", ".eex", ".heex", ".go", ".rs", ".rb", ".php", ".java",
    ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".sql", ".sh",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".txt", ".html", ".css",
    ".scss", ".vue", ".svelte",
}


def extract_keywords(feature: str) -> list[str]:
    """Lowercase word tokens (len>=3, unicode-aware) minus stopwords, plus an
    ASCII-folded variant of each (validação -> validacao); order-stable, deduped."""
    import unicodedata

    tokens = re.findall(r"\w{3,}", feature.lower())
    seen: dict[str, None] = {}
    for tok in tokens:
        if tok in _STOPWORDS or tok.isdigit():
            continue
        seen.setdefault(tok, None)
        folded = unicodedata.normalize("NFKD", tok).encode("ascii", "ignore").decode()
        if len(folded) >= 3 and folded not in _STOPWORDS:
            seen.setdefault(folded, None)
    return list(seen)


def _iter_files(repo_root: Path, allowed_prefixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for path in sorted(repo_root.rglob("*")):
        rel = path.relative_to(repo_root)
        parts = rel.parts
        if any(p in PACK_IGNORED_DIRS or p == ".git" or p.startswith(".") and p not in (".github",) for p in parts[:-1]):
            continue
        if not path.is_file():
            continue
        name = parts[-1]
        if name.startswith(".") and name not in (".gitignore", ".env.example"):
            continue
        if allowed_prefixes and not any(str(rel).startswith(pfx) for pfx in allowed_prefixes):
            continue
        if path.suffix.lower() not in _TEXT_EXTS:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
        if len(files) >= SCAN_FILE_CAP:
            break
    return files


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes()[:READ_CAP].decode("utf-8", errors="replace")
    except OSError:
        return ""


def score_file(rel: str, text: str, keywords: list[str]) -> int:
    """Deterministic relevance. Filename hits DOMINATE: a keyword in the file
    name is a far stronger signal than N occurrences inside a big generic file
    (which is how forms/bundles otherwise crowd out the actual target)."""
    if not keywords:
        return 0
    rel_lower = rel.lower()
    name = rel_lower.rsplit("/", 1)[-1]
    text_lower = text.lower()
    score = 0
    for kw in keywords:
        if kw in name:
            score += 40
        elif kw in rel_lower:
            score += 8
        score += min(text_lower.count(kw), 10)
    return score


# Languages where nesting is brace-based: only TOP-LEVEL lines are signatures
# (an indented `const a = 1;` inside a body is not). Python/Elixir keep the
# lstrip match so methods inside classes/modules still surface.
_TOP_LEVEL_ONLY = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs"}


def skeleton(text: str, suffix: str) -> str:
    """Signature-level view of a file: imports/exports/defs, not bodies."""
    prefixes = _SKELETON_PREFIXES.get(suffix)
    lines = text.splitlines()
    if prefixes is None:
        kept = lines[:30]
    elif suffix in _TOP_LEVEL_ONLY:
        kept = [ln for ln in lines if ln.startswith(prefixes)][:SKELETON_LINE_LIMIT]
    else:
        kept = [ln for ln in lines if ln.lstrip().startswith(prefixes)][:SKELETON_LINE_LIMIT]
    if not kept:
        kept = lines[:20]
    return "\n".join(ln[:160] for ln in kept)


def _tree(repo_root: Path, files: list[Path]) -> str:
    rels = sorted(str(f.relative_to(repo_root)) for f in files)[:TREE_LIMIT]
    suffix = "" if len(files) <= TREE_LIMIT else f"\n… (+{len(files) - TREE_LIMIT} more files)"
    return "\n".join(rels) + suffix


def build_pack(
    repo_root: Path,
    feature: str,
    *,
    allowed_prefixes: tuple[str, ...] = (),
    memory_text: str = "",
    budget: int = PACK_BUDGET,
) -> tuple[str, dict]:
    """Build the pack. Returns (pack_text, stats).

    stats: {"chars": int, "files_full": int, "files_skeleton": int, "scanned": int}
    """
    keywords = extract_keywords(feature)
    files = _iter_files(repo_root, tuple(allowed_prefixes))
    scored: list[tuple[int, str, Path, str]] = []
    for path in files:
        rel = str(path.relative_to(repo_root))
        text = _read_text(path)
        s = score_file(rel, text, keywords)
        if s > 0:
            scored.append((s, rel, path, text))
    scored.sort(key=lambda t: (-t[0], t[1]))

    header = (
        "CONTEXT PACK (pre-computed locally — trust it; open additional files "
        "ONLY if something you need is missing):\n"
    )
    sections: list[str] = [header]
    if memory_text:
        sections.append(f"== REPO MEMORY (learned from previous runs) ==\n{memory_text}\n")
    sections.append(f"== REPO FILE TREE ==\n{_tree(repo_root, files)}\n")

    used = sum(len(s) for s in sections)
    full_n = 0
    skel_n = 0
    for _score, rel, path, text in scored:
        if len(text) <= FULL_FILE_LIMIT:
            block = f"== FILE: {rel} (full) ==\n{text}\n"
            kind = "full"
        else:
            block = f"== FILE: {rel} (signatures only; open it for bodies) ==\n{skeleton(text, path.suffix.lower())}\n"
            kind = "skel"
        if used + len(block) > budget:
            continue
        sections.append(block)
        used += len(block)
        if kind == "full":
            full_n += 1
        else:
            skel_n += 1

    pack = "\n".join(sections)
    stats = {
        "chars": len(pack),
        "files_full": full_n,
        "files_skeleton": skel_n,
        "scanned": len(files),
    }
    return pack, stats
