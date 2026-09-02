"""Per-repo config + test-command detection, so the daily command is just
`shepherd-dev run "<feature>"` from inside the repo.

Config lives at <repo>/.shepherd-dev.json (committed by default — it is project
metadata, not local state). Stores `test_cmd` and `review_panel` today.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .remotegate import RemoteGateConfig

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

    # Rust — cargo test passes vacuously with 0 tests (exit 0), so only claim a
    # real suite when tests already exist; otherwise fall through to the native
    # gate (same "cargo test", with the test-writing hint + a no-test guard).
    if (repo_root / "Cargo.toml").is_file():
        if list(repo_root.glob("tests/**/*.rs")) or _rust_src_has_tests(repo_root):
            return "cargo test"
        return None

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


def _rust_src_has_tests(repo_root: Path) -> bool:
    """True when any Rust source already declares a test (#[test]/#[cfg(test)])."""
    for rs in repo_root.glob("src/**/*.rs"):
        try:
            text = rs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "#[test]" in text or "#[cfg(test)]" in text:
            return True
    return False


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
        # {EXUNIT_TESTS} is a presence sentinel (like Rust's {CARGO_TESTS}): a
        # `mix test` with no matching tests exits 0 (vacuous pass), so the gate
        # requires the proposal to actually ship an ExUnit test, else fails loudly.
        cmd = "mix test {EXUNIT_TESTS}"
        hint = (
            "Also write ExUnit tests for this feature under test/, in *_test.exs "
            "files using `use ExUnit.Case` (async: true when possible), runnable "
            "with `mix test`. They must assert the feature's behavior — a proposal "
            "with no ExUnit test is rejected. Test the feature's own modules directly; "
            "do NOT touch Ecto/the database unless the repo's test setup already provides it."
        )
        return cmd, hint
    if lang == "rust":
        # {CARGO_TESTS} is a presence sentinel — the gate strips it once it has
        # confirmed the proposal actually ships a Rust test, otherwise it fails
        # loudly. Needed because `cargo test` with 0 tests exits 0 (vacuous pass).
        cmd = "cargo test {CARGO_TESTS}"
        hint = (
            "Also write tests for this feature using Rust's BUILT-IN test framework "
            "(a `#[cfg(test)] mod tests { … }` block with `#[test]` functions in the "
            "same source file, or files under `tests/`), runnable with `cargo test`. "
            "They must actually assert the feature's behavior — a proposal with no "
            "test is rejected. Use only the standard library and the crate's own code."
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


#: The limits a repo may set instead of repeating them on every command line.
#: Flags still win; these only fill in what the command line left unsaid.
RUN_LIMIT_KEYS = ("worker_budget", "gate_timeout", "max_attempts")


def run_limits(repo_root: Path) -> dict[str, int]:
    """`worker_budget`, `gate_timeout`, `max_attempts` from the repo's
    `.shepherd-dev.json` (over the global config), as positive ints. A value
    that is not a positive int is ignored — the flag's own validation would
    have refused it, and a config must not do worse than a flag."""
    out: dict[str, int] = {}
    for source in (load_global_config(), load_config(repo_root)):  # repo wins
        block = source.get("limits")
        if not isinstance(block, dict):
            continue
        for key in RUN_LIMIT_KEYS:
            value = block.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                continue
            out[key] = value
    return out


#: The gate-pass rate at which running the reviewer alongside the gate pays:
#: above it, the review tokens a failed gate would waste are the rare case
#: and the review latency hidden behind the gate is the common one.
SPECULATIVE_REVIEW_THRESHOLD = 0.7


def speculative_review_config(repo_root: Path) -> str:
    """`speculative_review`: "on", "off" or "auto" (default). Auto turns it
    on when this repo's recent gate-pass rate (history.gate_pass_rate) is at
    least SPECULATIVE_REVIEW_THRESHOLD over at least three judged attempts."""
    out = "auto"
    for source in (load_global_config(), load_config(repo_root)):
        value = source.get("speculative_review")
        if isinstance(value, bool):
            out = "on" if value else "off"
        elif isinstance(value, str) and value.strip().lower() in ("on", "off", "auto"):
            out = value.strip().lower()
    return out


def preflight_config(repo_root: Path) -> dict:
    """`preflight` block: `auth_probe` (bool, default False) runs the auth
    probe on every claude run instead of only when the token needs a
    refresh — one short model call that turns a spent allowance into a
    refusal before the pack, the adoption and the worker are paid for."""
    out = {"auth_probe": False}
    for source in (load_global_config(), load_config(repo_root)):  # repo wins
        block = source.get("preflight")
        if isinstance(block, dict) and isinstance(block.get("auth_probe"), bool):
            out["auth_probe"] = block["auth_probe"]
    return out


def auto_optimize_config(repo_root: Path) -> dict | None:
    """auto_optimize settings; per-repo .shepherd-dev.json wins over the global
    ~/.shepherd-dev/config.json. None = feature off (the default)."""
    for source in (load_config(repo_root), load_global_config()):
        cfg = source.get("auto_optimize")
        if isinstance(cfg, dict):
            return cfg
    return None


def planning_config(repo_root: Path) -> dict:
    """Planning-prefetch (#4) settings; per-repo .shepherd-dev.json wins over the
    global config. Returns {"enabled": bool, "model": str} — enabled by default
    with a cheap model, so `run` gets the prefetch with no setup."""
    from .planning import DEFAULT_PLAN_MODEL

    out = {"enabled": True, "model": DEFAULT_PLAN_MODEL}
    for source in (load_global_config(), load_config(repo_root)):  # repo wins (last)
        # `models.planner.model` is the same choice under the per-role key the
        # worker and reviewer use (see launch.RoleModels); `planning.model`
        # stays honoured and, being the specific key, wins within one file.
        models = source.get("models")
        planner = models.get("planner") if isinstance(models, dict) else None
        if isinstance(planner, str):
            planner = {"model": planner}
        if isinstance(planner, dict):
            model = planner.get("model")
            if isinstance(model, str) and model.strip():
                out["model"] = model.strip()
        cfg = source.get("planning")
        if isinstance(cfg, dict):
            if "enabled" in cfg:
                out["enabled"] = bool(cfg["enabled"])
            model = cfg.get("model")
            if isinstance(model, str) and model.strip():
                out["model"] = model.strip()
    return out


#: Variables a repo may NOT set through jail_env. Every one of them redirects
#: an interpreter or its import path — the exact class supervisor scrubs in
#: CHILD_PYTHON_ENV_STRIP / GATE_ENV_STRIP, because a wrong PYTHONHOME stops
#: the interpreter booting before any of our code runs and a leaked PYTHONPATH
#: puts the supervisor's own source on the sandbox's sys.path. Letting a repo
#: config reintroduce them would undo those guarantees from the outside.
JAIL_ENV_REFUSED = frozenset({
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV",
    "PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS", "__PYVENV_LAUNCHER__",
})


def jail_env(repo_root: Path) -> dict[str, str]:
    """The repo's declared `jail_env`, ready to put in the environment.

    The worker's jail is materialized from a git tree, so everything
    gitignored is absent by construction: deps/, _build/, node_modules/,
    target/, .venv/. A worker on a compiled language therefore cannot compile,
    and errors a compiler reports in seconds cost a whole attempt plus a gate.
    `jail_env` lets the repo point its toolchain at a cache that lives outside
    the clone — measured on a Phoenix repo: a git-archive checkout with no
    deps/ compiles in 42s with MIX_DEPS_PATH set, and the cache is only read.

    Values are taken verbatim except for a leading `~`, which is expanded here
    because nothing between this and the worker's process will do it — the
    value travels as an environment variable, not as shell input. A relative
    value stays relative, since it means "inside the clone" and the clone is
    the worker's working directory.
    """
    raw = load_config(repo_root).get("jail_env")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key in JAIL_ENV_REFUSED:
            continue
        out[key] = str(Path(value).expanduser()) if value.startswith("~") else value
    return out


def pre_gate_cmd(repo_root: Path) -> str | None:
    """A fixer to run over the proposal before the gate judges it.

    For the class of failure a formatter repairs in under a second and a
    worker keeps reintroducing: observed on a real run where two of three
    attempts died on `mix format --check-formatted`, the second one AFTER
    the worker had been told about the first. Guidance does not hold it.
    """
    value = load_config(repo_root).get("pre_gate_cmd")
    return value if isinstance(value, str) and value.strip() else None


def jail_seed(repo_root: Path) -> dict[str, str]:
    """The repo's declared `jail_seed`, as {ENV_VAR: warm origin path}.

    jail_env is enough for a cache that is only READ — a dependency tree.
    A build cache is written, so pointing every run at one shared directory
    means two writers the moment a human compiles locally while a worker
    runs. Seeding hands each run its own copy instead, and the origin stays a
    clean warm baseline nothing writes to.

    Reaches the GATE and pre_gate_cmd, NOT the worker. Measured from a real
    run's journal: a jailed worker reads outside its clone (it listed the
    cache) but writes nowhere else — `touch` on the cache and `mkdir /tmp/x`
    both came back "Operation not permitted". The seeded copy is a temp
    directory outside every clone, so a worker handed this variable finds it
    unwritable and burns the attempt discovering that. jail_env survives the
    jail because reading is all it needs; this does not.
    """
    raw = load_config(repo_root).get("jail_seed")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key in JAIL_ENV_REFUSED:  # this sets an env var too — same guard
            continue
        out[key] = str(Path(value).expanduser())
    return out


def _clone_dir_fast(src: Path, dest: Path) -> bool:
    """APFS clone of a directory tree: copy-on-write, so a 56M build cache
    costs half a second and no disk until it diverges. True when it worked.

    Only APFS has clonefile; everywhere else this fails and the caller copies.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["cp", "-c", "-R", str(src), str(dest)],
            capture_output=True, timeout=300,
        )
        return proc.returncode == 0 and dest.exists()
    except Exception:
        return False


def _copy_tree(src: Path, dest: Path) -> None:
    """Seed `dest` from `src`, cheaply where the filesystem allows it."""
    import shutil

    if _clone_dir_fast(src, dest):
        return
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)


@contextlib.contextmanager
def jail_seed_applied(repo_root: Path):
    """Give this run its own copy of each declared warm cache.

    Each key becomes an environment variable pointing at a fresh copy, and the
    copy is destroyed when the run ends — so concurrent lanes cannot corrupt
    each other and the origin is never written. A missing origin yields an
    empty directory rather than an error: the first run, before any warm cache
    exists, must still work (the toolchain simply builds cold into it).

    Whether a cache survives being copied to a new path is the toolchain's
    business, not shepherd's. Measured: an Elixir _build does (4.76s against
    38.24s cold, source at a new path too). A Python venv does NOT — its
    shebangs hold absolute paths and keep pointing at the origin. The manual
    says so; this code stays language-agnostic.
    """
    import shutil
    import tempfile

    origins = jail_seed(repo_root)
    if not origins:
        yield
        return
    previous = {key: os.environ.get(key) for key in origins}
    root = Path(tempfile.mkdtemp(prefix="shepherd-seed-"))
    try:
        for key, origin in origins.items():
            target = root / key
            src = Path(origin)
            if src.is_dir():
                _copy_tree(src, target)
            target.mkdir(parents=True, exist_ok=True)
            os.environ[key] = str(target)
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        shutil.rmtree(root, ignore_errors=True)


@contextlib.contextmanager
def jail_env_applied(repo_root: Path):
    """Put the repo's jail_env in os.environ for the duration of the block.

    The substrate has no per-run environment hook — RuntimeOptions carries
    trace, provider and model and nothing else — so the values reach the
    worker the only way left: the provider spawns it as a child of this
    process. Which also means they reach every other child this command
    spawns, the gate included. That is why JAIL_ENV_REFUSED exists, and why a
    repo pointing its toolchain at a dependency cache is the intended use: the
    gate needs the same cache to be honest about the same tree.
    """
    values = jail_env(repo_root)
    if not values:
        yield
        return
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def remote_gate(repo_root: Path) -> "RemoteGateConfig | None":
    """Parse the repo's `test_remote` config into a RemoteGateConfig, or None.

    Generic: shepherd knows no service/DB/toolchain — the user's config carries
    the ssh target, warm repo dir, and the setup/test/teardown commands."""
    raw = load_config(repo_root).get("test_remote")
    if not isinstance(raw, dict):
        return None
    from .remotegate import parse_remote_config

    return parse_remote_config(raw, detect_language(repo_root))
