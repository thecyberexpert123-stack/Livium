"""Guards on the test suite itself.

A test that never runs is worse than a missing test: the suite reports success and
nobody notices. These checks were added after a rewritten module silently lost one
test method (a clobbered class body) while the suite still reported OK, and after
the same module briefly contained two copies of several classes.

They read the test sources as source, not by import, so a file that fails to import
is reported by the suite's own error handling rather than hidden here.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "tests"


def parse_all() -> dict[str, ast.Module]:
    modules = {}
    for path in sorted(TESTS.glob("test_*.py")):
        modules[str(path)] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return modules


class CollectionTests(unittest.TestCase):
    def test_no_test_module_defines_the_same_class_twice(self) -> None:
        for path, tree in parse_all().items():
            names = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
            duplicates = {name for name in names if names.count(name) > 1}
            self.assertFalse(
                duplicates,
                f"{pathlib.Path(path).name} defines {sorted(duplicates)} more than once; the "
                f"later definition silently replaces the earlier one")

    def test_every_test_case_class_has_at_least_one_test_method(self) -> None:
        for path, tree in parse_all().items():
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                bases = {getattr(base, "id", "") or getattr(base, "attr", "")
                         for base in node.bases}
                if "TestCase" not in bases and "LiviumTestCase" not in bases:
                    continue
                if any(decorator_name(dec) == "skip" for dec in node.decorator_list):
                    continue
                methods = [child.name for child in node.body
                           if isinstance(child, ast.FunctionDef)
                           and child.name.startswith("test")]
                if not methods and is_fixture_base(node, tree):
                    # A base class that exists to be inherited is a fixture, not a
                    # silently empty test class.
                    continue
                self.assertTrue(
                    methods,
                    f"{pathlib.Path(path).name}:{node.name} has no test methods, so unittest "
                    f"collects nothing from it")

    def test_discovery_collects_every_test_method_in_the_sources(self) -> None:
        expected = 0
        for path, tree in parse_all().items():
            bases = {node.name: base_names(node) for node in tree.body
                     if isinstance(node, ast.ClassDef)}
            for name, chain in bases.items():
                if not is_test_case(chain, bases):
                    continue
                node = next(n for n in tree.body
                            if isinstance(n, ast.ClassDef) and n.name == name)
                expected += sum(
                    1 for child in node.body
                    if isinstance(child, ast.FunctionDef) and child.name.startswith("test"))

        import sys
        import unittest as unittest_module

        sys.path.insert(0, str(TESTS))
        try:
            suite = unittest_module.defaultTestLoader.discover(
                str(TESTS), top_level_dir=str(TESTS))
        finally:
            sys.path.remove(str(TESTS))
        collected = suite.countTestCases()
        self.assertEqual(
            collected, expected,
            f"unittest discovery collects {collected} tests but the sources define {expected} "
            f"test methods: something is defined in a way unittest cannot run")


def base_names(node: ast.ClassDef) -> list[str]:
    return [(getattr(base, "id", "") or getattr(base, "attr", "")) for base in node.bases]


def is_test_case(names: list[str], known: dict[str, list[str]], seen: frozenset[str] = frozenset()
                 ) -> bool:
    """Walk the ancestor chain of a class to find unittest.TestCase."""

    for name in names:
        if name in ("TestCase", "LiviumTestCase"):
            return True
        if name in known and name not in seen:
            if is_test_case(known[name], known, seen | {name}):
                return True
    return False


def is_fixture_base(node: ast.ClassDef, tree: ast.Module) -> bool:
    """True when another class in the same module inherits from ``node``."""

    for other in tree.body:
        if not isinstance(other, ast.ClassDef) or other is node:
            continue
        for base in other.bases:
            name = getattr(base, "id", "") or getattr(base, "attr", "")
            if name == node.name:
                return True
    return False


def decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return decorator_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
