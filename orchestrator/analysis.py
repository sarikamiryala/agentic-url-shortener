"""Static codebase analysis used for brownfield impact analysis.

Everything here is derived from the actual files in the workspace using `ast`:
modules, classes, functions, HTTP routes, SQL schema, an approximate call graph and
a tests inventory. Calls are matched by *name* (not by type inference), so the call
graph is an over-approximation; that is fine for "what might this change touch".
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

HTTP_METHODS = {"get", "post", "put", "delete", "patch"}
_SCHEMA_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;", re.S | re.I)
_CONSTRAINT_WORDS = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}
_COVERS_RE = re.compile(r"^#\s*covers:\s*(.+)$", re.M)


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.stack: list[str] = []
        self.functions: dict[str, set[str]] = {}  # qualname -> called simple names
        self.classes: list[str] = []
        self.routes: list[dict] = []
        self.used_names: set[str] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qual = ".".join([self.module, *self.stack, node.name])
        calls: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = _called_name(child)
                if name:
                    calls.add(name)
        self.functions[qual] = calls
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in HTTP_METHODS
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
                and isinstance(dec.args[0].value, str)
            ):
                self.routes.append(
                    {
                        "method": dec.func.attr.upper(),
                        "path": dec.args[0].value,
                        "handler": qual,
                        "module": self.module,
                    }
                )
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Name(self, node: ast.Name) -> None:
        self.used_names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.used_names.add(node.attr)
        self.generic_visit(node)


def _internal_imports(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level >= 1 and node.module:
            out.append(node.module.split(".")[0])
    return sorted(set(out))


def parse_schema(text: str) -> dict[str, list[str]]:
    tables: dict[str, list[str]] = {}
    for name, body in _SCHEMA_RE.findall(text):
        cols = []
        for line in body.split(","):
            tokens = line.strip().split()
            if tokens and tokens[0].upper() not in _CONSTRAINT_WORDS:
                cols.append(tokens[0])
        tables[name] = cols
    return tables


def scan_codebase(root: Path, package: str = "shortener") -> dict:
    root = Path(root)
    modules: dict[str, dict] = {}
    routes: list[dict] = []
    schema: dict[str, list[str]] = {}
    functions: dict[str, set[str]] = {}
    defined_in: dict[str, set[str]] = {}
    used_in: dict[str, set[str]] = {}

    for path in sorted((root / package).rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        module = path.stem
        v = _Visitor(module)
        v.visit(tree)
        modules[module] = {
            "path": path.relative_to(root).as_posix(),
            "loc": len(text.splitlines()),
            "classes": sorted(v.classes),
            "functions": sorted(q.split(".", 1)[1] for q in v.functions),
            "imports": _internal_imports(tree),
        }
        routes.extend(v.routes)
        schema.update(parse_schema(text))
        functions.update(v.functions)
        for qual in v.functions:
            defined_in.setdefault(qual.split(".")[-1], set()).add(module)
        for cls in v.classes:
            defined_in.setdefault(cls, set()).add(module)
        for name in v.used_names:
            used_in.setdefault(name, set()).add(module)

    # name -> qualified definitions, for resolving call edges
    by_simple: dict[str, list[str]] = {}
    for qual in functions:
        by_simple.setdefault(qual.split(".")[-1], []).append(qual)

    flows = []
    for r in routes:
        edges: list[str] = []
        seen = {r["handler"]}
        frontier = [r["handler"]]
        for _ in range(4):  # depth limit keeps this readable
            nxt: list[str] = []
            for caller in frontier:
                for name in sorted(functions.get(caller, ())):
                    for callee in by_simple.get(name, []):
                        if callee not in seen:
                            seen.add(callee)
                            edges.append(f"{caller} -> {callee}")
                            nxt.append(callee)
            frontier = nxt
        flows.append({"route": f"{r['method']} {r['path']}", "handler": r["handler"], "calls": edges})

    tests = []
    for path in sorted((root / "tests").rglob("test_*.py")) if (root / "tests").exists() else []:
        text = path.read_text(encoding="utf-8")
        covers = sorted({c.strip() for m in _COVERS_RE.findall(text[:600]) for c in m.split(",") if c.strip()})
        tests.append(
            {
                "file": path.relative_to(root).as_posix(),
                "tests": len(re.findall(r"^\s*def test_", text, re.M)),
                "covers": covers,
            }
        )

    return {
        "modules": modules,
        "routes": sorted(routes, key=lambda r: (r["path"], r["method"])),
        "schema": schema,
        "flows": flows,
        "tests": tests,
        "defined_in": {k: sorted(v) for k, v in defined_in.items()},
        "used_in": {k: sorted(v) for k, v in used_in.items()},
    }


def symbol_footprint(scan: dict, symbol: str) -> dict:
    return {
        "symbol": symbol,
        "defined_in": scan["defined_in"].get(symbol, []),
        "used_in": scan["used_in"].get(symbol, []),
    }
