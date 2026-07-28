"""No module may reference a global name that does not exist.

cli.py used `os.readlink` in a branch nothing exercised, and never imported os.
It sat there until a new test happened to walk that branch — a NameError
waiting for the first user whose dirty worktree contained a symlink.

Tests cannot cover every branch, and a missing import is not really a testing
gap: the name is either resolvable or it is not, and that is decidable without
running anything. So decide it. `symtable` gives the interpreter's OWN scope
analysis — nested functions, comprehensions, class bodies, globals and
nonlocals — so this is not a hand-rolled approximation of Python's rules.

Runnable with: python -m unittest tests.test_undefined_names
"""

from __future__ import annotations

import builtins
import symtable
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

#: Names the analysis cannot see. `__file__` and friends are injected by the
#: import machinery rather than bound in the source. Python 3.14 (PEP 649/749)
#: adds `__conditional_annotations__`: the compiler reads it wherever
#: annotations live under a conditional block, and binds it implicitly — it
#: never appears in the source, so it must not count as an unbound global.
_MODULE_DUNDERS = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__path__", "__debug__",
    "__conditional_annotations__",
}


def _bound_at_module_level(table: symtable.SymbolTable) -> set[str]:
    out = set(_MODULE_DUNDERS) | set(dir(builtins))
    for sym in table.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            out.add(sym.get_name())
    return out


def _free_names(table: symtable.SymbolTable, known: set[str], path: str) -> list[str]:
    """Names read from this scope that resolve to nothing above it."""
    bad: list[str] = []
    for sym in table.get_symbols():
        name = sym.get_name()
        if name in known:
            continue
        # Referenced, and neither local to this scope nor bound anywhere the
        # interpreter would look. is_global() is symtable's own verdict.
        if sym.is_referenced() and sym.is_global() and not sym.is_assigned():
            bad.append(f"{path}: {name}")
    for child in table.get_children():
        bad += _free_names(child, known, f"{path}.{child.get_name()}")
    return bad


class NoUndefinedGlobals(unittest.TestCase):
    def test_every_module_resolves_every_name_it_reads(self):
        modules = sorted((SRC / "shepherd_dev").rglob("*.py"))
        self.assertGreater(len(modules), 10, "the package went missing")

        offenders: list[str] = []
        for path in modules:
            source = path.read_text(encoding="utf-8")
            top = symtable.symtable(source, str(path), "exec")
            known = _bound_at_module_level(top)
            rel = path.relative_to(SRC).as_posix()
            offenders += _free_names(top, known, rel)

        self.assertEqual(
            offenders, [],
            "these names are read but never bound — a NameError waiting for the "
            "first caller to reach that branch:\n  " + "\n  ".join(offenders),
        )

    def test_the_check_catches_a_missing_import(self):
        """A check that cannot fail proves nothing. This is the cli.py defect."""
        source = "def f(p):\n    return os.readlink(p)\n"
        top = symtable.symtable(source, "<probe>", "exec")
        found = _free_names(top, _bound_at_module_level(top), "probe")
        self.assertEqual(found, ["probe.f: os"])

    def test_the_check_accepts_a_function_local_import(self):
        """Half this package imports inside functions; none of that is a defect."""
        source = "def f(p):\n    import os\n    return os.readlink(p)\n"
        top = symtable.symtable(source, "<probe>", "exec")
        self.assertEqual(_free_names(top, _bound_at_module_level(top), "probe"), [])

    def test_the_check_accepts_comprehensions_and_nested_scopes(self):
        source = (
            "import os\n"
            "def f(items):\n"
            "    seen = {x for x in items if x}\n"
            "    def g():\n"
            "        return seen, os.sep\n"
            "    return g()\n"
        )
        top = symtable.symtable(source, "<probe>", "exec")
        self.assertEqual(_free_names(top, _bound_at_module_level(top), "probe"), [])


if __name__ == "__main__":
    unittest.main()
