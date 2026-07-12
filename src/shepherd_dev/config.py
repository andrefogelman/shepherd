"""Per-repo config + test-command detection, so the daily command is just
`shepherd-dev run "<feature>"` from inside the repo.

Config lives at <repo>/.shepherd-dev.json (committed by default — it is project
metadata, not local state). Only `test_cmd` is stored today.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_NAME = ".shepherd-dev.json"


def find_repo_root(start: Path | None = None) -> Path | None:
    """Nearest ancestor (inclusive) that is a Shepherd workspace (has .vcscore)."""
    cur = (start or Path.cwd()).resolve()
    for path in (cur, *cur.parents):
        if (path / ".vcscore").is_dir():
            return path
    return None


def load_config(repo_root: Path) -> dict:
    path = repo_root / CONFIG_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(repo_root: Path, updates: dict) -> None:
    path = repo_root / CONFIG_NAME
    merged = {**load_config(repo_root), **updates}
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def detect_test_cmd(repo_root: Path) -> str | None:
    """Best-effort test command from project files. None if nothing recognized."""
    # Node — only when package.json actually declares a test script
    pkg = repo_root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except Exception:
            scripts = {}
        if isinstance(scripts, dict) and scripts.get("test"):
            mgr = (
                "pnpm" if (repo_root / "pnpm-lock.yaml").is_file()
                else "yarn" if (repo_root / "yarn.lock").is_file()
                else "bun" if (repo_root / "bun.lockb").is_file() or (repo_root / "bun.lock").is_file()
                else "npm"
            )
            return f"{mgr} test" if mgr != "npm" else "npm test"

    # Elixir
    if (repo_root / "mix.exs").is_file():
        return "mix test"

    # Rust
    if (repo_root / "Cargo.toml").is_file():
        return "cargo test"

    # Go
    if (repo_root / "go.mod").is_file():
        return "go test ./..."

    # Python — prefer pytest when there's any sign of it, else unittest discover
    py_signals = [
        repo_root / "pytest.ini",
        repo_root / "tox.ini",
        repo_root / "pyproject.toml",
        repo_root / "setup.cfg",
    ]
    if any(p.is_file() for p in py_signals) or list(repo_root.glob("test_*.py")) or (repo_root / "tests").is_dir():
        text = ""
        pp = repo_root / "pyproject.toml"
        if pp.is_file():
            try:
                text = pp.read_text(encoding="utf-8")
            except Exception:
                text = ""
        if "pytest" in text or any(p.is_file() for p in (repo_root / "pytest.ini", repo_root / "tox.ini")):
            return "pytest -q"
        return "pytest -q" if _pytest_available() else "python3 -m unittest discover"

    return None


def _pytest_available() -> bool:
    import shutil

    return shutil.which("pytest") is not None


def resolve_test_cmd(repo_root: Path, explicit: str | None) -> tuple[str | None, str]:
    """Return (test_cmd, source). Precedence: explicit flag > saved config > detection.
    source is one of 'flag' | 'config' | 'detected' | 'none'."""
    if explicit:
        return explicit, "flag"
    cfg = load_config(repo_root).get("test_cmd")
    if isinstance(cfg, str) and cfg.strip():
        return cfg, "config"
    detected = detect_test_cmd(repo_root)
    if detected:
        return detected, "detected"
    return None, "none"
