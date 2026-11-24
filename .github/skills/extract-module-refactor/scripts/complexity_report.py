#!/usr/bin/env python3
"""Cyclomatic-complexity report for Python files.

Usage: python complexity_report.py src/trading_bot.py [more files...]

Prints one line per function, sorted by complexity:
    CC <complexity> <lines>L L<first-line> <name>
plus a total line count per file. Matches the repo's ruff mccabe settings
closely enough to rank extraction candidates.
"""

from __future__ import annotations

import ast
import sys


class _Complexity(ast.NodeVisitor):
    def __init__(self) -> None:
        self.complexity = 1

    def visit_If(self, node: ast.If) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.complexity += len(node.handlers)
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.complexity += len(node.values) - 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.complexity += len(node.cases) - 1
        self.generic_visit(node)


def complexity_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    visitor = _Complexity()
    for child in ast.iter_child_nodes(node):
        visitor.visit(child)
    return visitor.complexity


def main(paths: list[str]) -> int:
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, filename=path)
        print(f"== {path}")
        rows = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                end = getattr(node, "end_lineno", node.lineno)
                rows.append(
                    (
                        complexity_of(node),
                        end - node.lineno + 1,
                        node.lineno,
                        node.name,
                    )
                )
        for cc, lines, lineno, name in sorted(rows, reverse=True):
            if cc >= 5:
                print(f"  CC {cc:<3} {lines:>4}L  L{lineno:<5} {name}")
        print(f"{len(source.splitlines())} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
