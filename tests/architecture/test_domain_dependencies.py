"""The domain can run without importing application or concrete adapter modules."""

import ast
from pathlib import Path


def test_domain_has_no_reverse_runtime_imports():
    root = Path(__file__).resolve().parents[2] / "reuleauxcoder" / "domain"
    forbidden = ("reuleauxcoder.app", "reuleauxcoder.extensions",
                 "reuleauxcoder.infrastructure", "reuleauxcoder.services")
    violations = []

    class Imports(ast.NodeVisitor):
        def visit_If(self, node):
            # Type-only legacy annotations are tracked separately from execution.
            if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                for child in node.orelse:
                    self.visit(child)
            else:
                self.generic_visit(node)

        def visit_ImportFrom(self, node):
            if (node.module or "").startswith(forbidden):
                violations.append(f"{path.relative_to(root)}:{node.lineno}: {node.module}")

        def visit_Import(self, node):
            for alias in node.names:
                if alias.name.startswith(forbidden):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}: {alias.name}")

    for path in root.rglob("*.py"):
        Imports().visit(ast.parse(path.read_text(encoding="utf-8")))
    assert not violations, "\n".join(violations)
