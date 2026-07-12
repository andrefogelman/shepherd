"""Per-repo config + test-command detection, so the daily command is just
`shepherd-dev run "<feature>"` from inside the repo.

Config lives at <repo>/.shepherd-dev.json (committed by default — it is project
metadata, not local state). Only `test_cmd` is stored today.
"""

from __future__ import annotations

import json
import os
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

    # Elixir — only claim a real suite when tests already exist; otherwise fall
    # through to the native gate (same "mix test", but carrying the ExUnit hint
    # so the worker writes tests).
    if (repo_root / "mix.exs").is_file():
        if list(repo_root.glob("test/**/*_test.exs")):
            return "mix test"
        return None

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


def detect_language(repo_root: Path) -> str | None:
    """Dominant language of the repo, for the native-gate fallback."""
    if (repo_root / "package.json").is_file() or list(repo_root.glob("**/tsconfig.json"))[:1]:
        return "js"
    if (repo_root / "mix.exs").is_file():
        return "elixir"
    if (repo_root / "Cargo.toml").is_file():
        return "rust"
    if (repo_root / "go.mod").is_file():
        return "go"
    if (
        list(repo_root.glob("*.py"))
        or (repo_root / "pyproject.toml").is_file()
        or (repo_root / "setup.py").is_file()
    ):
        return "python"
    return None


def _node_supports_strip_types() -> bool:
    """node --test can run .ts via strip-types on Node >= 22.6 (no deps)."""
    import subprocess

    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5).stdout
        v = out.strip().lstrip("v").split(".")
        major, minor = int(v[0]), int(v[1])
        return major > 22 or (major == 22 and minor >= 6)
    except Exception:
        return False


def _repo_is_typescript(repo_root: Path) -> bool:
    return (repo_root / "tsconfig.json").is_file() or bool(list(repo_root.glob("**/tsconfig.json"))[:1])


def native_gate(repo_root: Path, lang: str) -> tuple[str, str] | None:
    """(cmd, worker_hint) for the zero-dependency floor gate, or None."""
    if lang == "js":
        ts = _repo_is_typescript(repo_root)
        # Explicit `.test.` glob, NOT node --test's default patterns — those also
        # match `test-*.js`, which would sweep build artifacts (e.g. lucide
        # `test-tube*.js` icons under dist/) and fail the gate. node --test skips
        # node_modules on its own; scoping to *.test.* also avoids dist output.
        # {NEW_TESTS} is replaced by the gate with the *.test.* files FROM THE
        # PROPOSAL itself. Scoping to the proposal's own tests (not a repo-wide
        # glob) keeps the native gate honest: a repo without a runnable suite
        # may still contain tests written for other runners (e.g. a Vitest
        # compat.test.ts) that crash node --test with ERR_MODULE_NOT_FOUND.
        if ts and _node_supports_strip_types():
            cmd = "node --test --experimental-strip-types {NEW_TESTS}"
            files = "*.test.ts (TypeScript)"
        else:
            cmd = "node --test {NEW_TESTS}"
            files = "*.test.js / *.test.mjs (plain JavaScript, not TypeScript)"
        hint = (
            "Also write tests for this feature using Node's BUILT-IN test runner "
            f"(`import test from 'node:test'` + `node:assert`), in {files}. "
            f"They must pass with `{cmd}` and import ONLY the Node standard library "
            "and the feature's own files — no third-party packages, since node_modules "
            "may not be installed."
        )
        return cmd, hint
    if lang == "python":
        cmd = "python3 -m unittest {NEW_TESTS}"
        hint = (
            "Also write tests for this feature using Python's built-in unittest, in "
            "files named *_test.py (the gate runs exactly the test files you add). "
            "Import only the standard library and the feature's own modules — no "
            "third-party packages."
        )
        return cmd, hint
    if lang == "elixir":
        cmd = "mix test"
        hint = (
            "Also write ExUnit tests for this feature under test/, in *_test.exs "
            "files using `use ExUnit.Case` (async: true when possible), runnable "
            "with `mix test`. Test the feature's own modules directly; do NOT touch "
            "Ecto/the database unless the repo's test setup already provides it."
        )
        return cmd, hint
    return None


# ── ExUnit coverage guard (Elixir) ──────────────────────────────────────────
# ExUnit is not zero-dependency like node --test: `mix test` needs the ExUnit
# scaffold (test/test_helper.exs calling ExUnit.start()). `init` verifies it and
# generates a minimal one if missing, so the gate has somewhere to run.

def exunit_ready(repo_root: Path) -> bool:
    helper = repo_root / "test" / "test_helper.exs"
    try:
        return helper.is_file() and "ExUnit.start" in helper.read_text(encoding="utf-8")
    except Exception:
        return False


def ensure_exunit_scaffold(repo_root: Path) -> bool:
    """Create test/test_helper.exs (ExUnit.start()) if absent. Returns True if
    it generated the file, False if it was already present."""
    if exunit_ready(repo_root):
        return False
    (repo_root / "test").mkdir(parents=True, exist_ok=True)
    (repo_root / "test" / "test_helper.exs").write_text("ExUnit.start()\n", encoding="utf-8")
    return True


def resolve_test_cmd(repo_root: Path, explicit: str | None) -> tuple[str | None, str, str | None]:
    """Return (test_cmd, source, worker_hint).

    Precedence: explicit flag > saved config > project detection > native-gate
    fallback (a zero-dep runner chosen by language, with a hint that makes the
    worker write the tests itself). source in flag|config|detected|native|none.
    worker_hint is set only for the native fallback."""
    if explicit:
        return explicit, "flag", None
    cfg = load_config(repo_root).get("test_cmd")
    if isinstance(cfg, str) and cfg.strip():
        return cfg, "config", None
    detected = detect_test_cmd(repo_root)
    if detected and not _detected_gate_is_dead(repo_root, detected):
        return detected, "detected", None
    lang = detect_language(repo_root)
    if lang:
        gate = native_gate(repo_root, lang)
        if gate:
            return gate[0], "native", gate[1]
    return None, "none", None


def _detected_gate_is_dead(repo_root: Path, cmd: str) -> bool:
    """A node package-manager gate (npm/yarn/pnpm/bun test) cannot run without
    node_modules — treat it as unavailable so we fall back to the native gate."""
    first = cmd.split()[0] if cmd else ""
    if first in ("npm", "yarn", "pnpm", "bun"):
        return not (repo_root / "node_modules").is_dir()
    return False


GLOBAL_CONFIG = Path(
    os.environ.get("SHEPHERD_DEV_CONFIG") or Path.home() / ".shepherd-dev" / "config.json"
)


def load_global_config() -> dict:
    try:
        data = json.loads(GLOBAL_CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def auto_optimize_config(repo_root: Path) -> dict | None:
    """auto_optimize settings; per-repo .shepherd-dev.json wins over the global
    ~/.shepherd-dev/config.json. None = feature off (the default)."""
    for source in (load_config(repo_root), load_global_config()):
        cfg = source.get("auto_optimize")
        if isinstance(cfg, dict):
            return cfg
    return None
