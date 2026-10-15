"""Explicit dependency graph for a run.

Nodes are SDLC stages. Edges are `deps`. The graph is data, so the engine can
re-plan (insert or remove nodes) while a run is in flight, and the dashboard can
draw the exact graph that is executing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


class GraphError(Exception):
    pass


@dataclass
class NodeSpec:
    id: str
    title: str
    agent: str
    deps: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    entry_gates: list[str] = field(default_factory=list)
    exit_gates: list[str] = field(default_factory=list)
    approval: str | None = None  # None | "always" | "by_impact"
    max_retries: int = 2
    max_rework: int = 2
    fallback_agent: str | None = None
    rework_target: str | None = None  # stage to redo when this stage's gates fail
    dynamic: bool = False  # inserted by a re-plan rather than part of the base pipeline

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NodeSpec":
        return cls(**d)


class Graph:
    def __init__(self, nodes: list[NodeSpec] | None = None) -> None:
        self.nodes: dict[str, NodeSpec] = {}
        for n in nodes or []:
            self.add(n, validate=False)
        self.validate()

    # ---- construction -------------------------------------------------------------
    def add(self, spec: NodeSpec, validate: bool = True) -> None:
        if spec.id in self.nodes:
            raise GraphError(f"duplicate node {spec.id}")
        self.nodes[spec.id] = spec
        if validate:
            self.validate()

    def remove(self, node_id: str) -> None:
        if node_id not in self.nodes:
            raise GraphError(f"unknown node {node_id}")
        del self.nodes[node_id]
        for spec in self.nodes.values():
            if node_id in spec.deps:
                spec.deps.remove(node_id)
        self.validate()

    def validate(self) -> None:
        for spec in self.nodes.values():
            for d in spec.deps:
                if d not in self.nodes:
                    raise GraphError(f"{spec.id} depends on unknown node {d}")
            if spec.rework_target and spec.rework_target not in self.nodes:
                raise GraphError(f"{spec.id} has unknown rework target {spec.rework_target}")
        self.topo_order()  # raises on cycles

    # ---- queries ------------------------------------------------------------------
    def topo_order(self) -> list[str]:
        indeg = {n: len(s.deps) for n, s in self.nodes.items()}
        ready = [n for n, d in indeg.items() if d == 0]
        order: list[str] = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for c in self.children(n):
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        if len(order) != len(self.nodes):
            stuck = sorted(set(self.nodes) - set(order))
            raise GraphError(f"cycle detected among {stuck}")
        return order

    def children(self, node_id: str) -> list[str]:
        return [n for n, s in self.nodes.items() if node_id in s.deps]

    def descendants(self, node_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            for c in self.children(stack.pop()):
                if c not in seen:
                    seen.add(c)
                    stack.append(c)
        return seen

    def ancestors(self, node_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            for d in self.nodes[stack.pop()].deps:
                if d not in seen:
                    seen.add(d)
                    stack.append(d)
        return seen

    def layers(self) -> list[list[str]]:
        """Longest-path layering: nodes in one layer may run in parallel."""
        depth: dict[str, int] = {}
        for n in self.topo_order():
            depth[n] = 1 + max((depth[d] for d in self.nodes[n].deps), default=-1)
        out: list[list[str]] = [[] for _ in range(max(depth.values(), default=-1) + 1)]
        for n, d in depth.items():
            out[d].append(n)
        return out

    def edges(self) -> list[tuple[str, str]]:
        return [(d, n) for n, s in self.nodes.items() for d in s.deps]

    # ---- (de)serialisation --------------------------------------------------------
    def to_dict(self) -> dict:
        return {"nodes": [s.to_dict() for s in self.nodes.values()]}

    @classmethod
    def from_dict(cls, d: dict) -> "Graph":
        return cls([NodeSpec.from_dict(n) for n in d["nodes"]])
