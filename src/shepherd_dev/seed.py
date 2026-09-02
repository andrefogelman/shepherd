"""A toolchain the jailed worker can USE — per launch, writable, outside the
clone.

The jail's working copy is materialized from a git tree, so everything the
repository gitignores is absent by construction: `deps/`, `_build/`,
`node_modules/`, `target/`. A worker on a compiled language therefore could
not compile, and a defect the compiler names in seconds cost a whole attempt
and a gate to discover. `jail_env` and `jail_seed` gave the GATE a warm cache
(the gate runs unjailed, from the parent); the worker could read the same
cache and write nowhere — the jail denies every write outside its working
copy — so it burned turns finding that out, and twenty-odd workers then
shipped the repository to a remote host to compile it there.

The jail can have more than one writable root. So, per launch:

- each `jail_seed` origin is cloned (copy-on-write where the filesystem has
  it) into a fresh temporary directory, that directory is added to the
  launch's writable roots, and the variable names it — for THIS launch
  only, through the env prefix the substrate already puts before the CLI;
- each `jail_seed_links` entry (a path inside the tree → its warm origin,
  `node_modules` being the case) is cloned the same way and a symlink at
  that path in the working copy points to the copy; the link is removed
  after the launch and its path never enters the changeset.

Every launch — each worker attempt, the reviewer — gets its own copies, so
two lanes, a worker and a gate, or an attempt and its retry never write to
one directory. The copies are destroyed when the launch returns.
"""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SeedPlan:
    """What one launch was given, and how to take it back."""

    env: dict[str, str] = field(default_factory=dict)
    links: list[tuple[str, Path]] = field(default_factory=list)
    roots: tuple[str, ...] = ()
    _tmp: Path | None = None

    def cleanup(self, working_path: Path | str | None = None) -> None:
        if working_path is not None:
            for rel, _target in self.links:
                link = Path(working_path) / rel
                try:
                    if link.is_symlink():
                        link.unlink()
                except OSError:
                    pass
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def describe(self) -> dict:
        return {
            "env": sorted(self.env),
            "links": [rel for rel, _ in self.links],
            "writable_roots": len(self.roots),
        }


@dataclass(frozen=True)
class LaunchSeed:
    """The repo's declared seeds, ready to be dealt out per launch."""

    env_origins: Mapping[str, str] = field(default_factory=dict)
    link_origins: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, repo_root: Path | None) -> "LaunchSeed":
        if repo_root is None:
            return cls()
        from .config import jail_seed, jail_seed_links

        return cls(env_origins=dict(jail_seed(repo_root)), link_origins=dict(jail_seed_links(repo_root)))

    @property
    def empty(self) -> bool:
        return not self.env_origins and not self.link_origins

    def prepare(self, working_path: Path | str) -> SeedPlan:
        """Clone every origin for one launch. A missing origin yields an
        empty directory (the first run, before any warm cache exists, still
        works: the toolchain builds cold into it)."""
        plan = SeedPlan()
        if self.empty:
            return plan
        from .config import _copy_tree

        tmp = Path(tempfile.mkdtemp(prefix="shepherd-launchseed-"))
        plan._tmp = tmp
        roots: list[str] = []
        for key, origin in self.env_origins.items():
            target = tmp / "env" / key
            src = Path(origin)
            if src.is_dir():
                _copy_tree(src, target)
            target.mkdir(parents=True, exist_ok=True)
            plan.env[key] = str(target)
            roots.append(str(target))
        work = Path(working_path)
        for rel, origin in self.link_origins.items():
            target = tmp / "links" / rel.replace("/", "__")
            src = Path(origin)
            if src.is_dir():
                _copy_tree(src, target)
            target.mkdir(parents=True, exist_ok=True)
            link = work / rel
            try:
                if link.is_symlink() or link.exists():
                    # a tracked directory of the same name would be shadowed
                    # by the link and restored by cleanup; a stray file too
                    if link.is_dir() and not link.is_symlink():
                        shutil.rmtree(link)
                    else:
                        link.unlink()
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                continue
            plan.links.append((rel, target))
            roots.append(str(target))
        plan.roots = tuple(roots)
        return plan


def _is_env_binary(item: object) -> bool:
    text = str(item)
    return text == "env" or text.endswith("/env")


def seed_argv(argv: list, env: Mapping[str, str]) -> list:
    """Assign the plan's variables for this launch only: `KEY=value` items
    right after the env binary the substrate already puts before the CLI,
    ahead of its own assignments. No env binary in the argv — nothing to
    hang them on — leaves the argv untouched."""
    if not env:
        return list(argv)
    out = list(argv)
    for index, item in enumerate(out):
        if _is_env_binary(item):
            out[index + 1:index + 1] = [f"{k}={v}" for k, v in sorted(env.items())]
            return out
    return out


def widen_confinement(spec, roots: Iterable[str]):
    """The same spec with `roots` added to its writable roots. A spec with
    no writable roots (a read-only launch) is left read-only; a spec this
    code does not recognise is returned as it came."""
    roots = tuple(r for r in roots if r)
    if not roots:
        return spec
    current = getattr(spec, "writable_roots", None)
    if not isinstance(current, (tuple, list)) or not current:
        return spec
    try:
        return dataclasses.replace(spec, writable_roots=tuple(current) + roots)
    except Exception:
        return spec


def link_paths(seed: "LaunchSeed | None") -> tuple[str, ...]:
    """The tree paths the links occupy — never part of a proposal."""
    if seed is None:
        return ()
    return tuple(seed.link_origins)
