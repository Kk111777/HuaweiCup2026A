"""计算 DAG、张量索引及式 (1)—(5) 的静态量。"""
from __future__ import annotations

from dataclasses import dataclass
from .model import Graph

from . import official

PIPES = ("PIPE_M", "PIPE_V")


def ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


@dataclass(frozen=True)
class Tensor:
    size: int
    pos: str
    producer: int | None
    consumers: frozenset[int]
    final_output: bool
    copy_cycles: int


class GraphIndex:
    def __init__(self, graph: dict, settings: official.Settings):
        self.model = graph if isinstance(graph, Graph) else Graph(graph)
        self.graph = self.model.raw
        graph = self.graph
        self.settings = settings
        all_ops = self.model.all_ops
        self.ops = {v: self.model.ops[v] for v in sorted(self.model.ops)}
        self.ids = tuple(self.ops)
        for v, op in self.ops.items():
            if op["pipe"] not in PIPES:
                raise ValueError(f"initialization model requires PIPE_M/PIPE_V: op {v}")
        self.preds, self.succs = self.model.pred, self.model.succ
        self.inputs, self.outputs = self.model.inputs, self.model.outputs
        producers, consumers = self.model.producers, self.model.consumers
        self.tensors = {}
        for t in graph["tensors"]:
            tid = t["id"]
            ps = producers[tid] & self.ops.keys()
            us = consumers[tid] & self.ops.keys()
            if not ps and not us:
                continue
            if len(ps) > 1:
                raise ValueError(f"initialization model requires a single producer: tensor {tid}")
            final = not us or any(all_ops[u]["op"] == "COPY_OUT" for u in consumers[tid])
            # 与官方 Task 构造一致：直接触及的 DDR tensor 转为核内 UB 副本。
            pos = "UB" if t["pos"] == "DDR" else t["pos"]
            self.tensors[tid] = Tensor(t["size"], pos, next(iter(ps), None),
                                       frozenset(us), final,
                                       max(1, ceil_div(t["size"], settings.bandwidth)))
        self.footprint = {}
        for v in self.ids:
            sizes = dict.fromkeys(settings.capacity, 0)
            for tid in self.inputs[v] | self.outputs[v]:
                t = self.tensors[tid]
                sizes[t.pos] += t.size
            if any(sizes[p] > settings.capacity[p] for p in sizes):
                raise ValueError(f"op {v} inputs and outputs exceed capacity: {sizes}")
            self.footprint[v] = sizes
        self.order = self.model.order
        if all(self.cycles(v) == self.model.cost[v] for v in self.ids):
            self.a, self.b = self.model.forward, self.model.backward
        else:
            # Preserve the collaborator's raw-zero heuristic on zero-cycle ops.
            # The shared official-cost model still clamps them to one cycle.
            self.a, self.b = {}, {}
            for v in self.order:
                self.a[v] = self.cycles(v) + max((self.a[u] for u in self.preds[v]), default=0)
            for v in reversed(self.order):
                self.b[v] = self.cycles(v) + max((self.b[u] for u in self.succs[v]), default=0)
        self.tail = {v: max((self.b[u] for u in self.succs[v]), default=0) for v in self.ids}
        self.single_r = {v: max((self.a[u] for u in self.preds[v]), default=0) for v in self.ids}
        self.single_q = {v: sum(self.tensors[t].copy_cycles
                                for t in self.inputs[v] | self.outputs[v]) for v in self.ids}
        self.single_d = {v: self.cycles(v) + self.single_q[v]
                            + self.pressure(self.footprint[v]) for v in self.ids}
        self.components = self.model.components
        self.work = self.workload(self.ids)

    def cycles(self, v: int) -> int:
        return self.ops[v]["cycles"]

    def workload(self, nodes) -> dict[str, int]:
        result = dict.fromkeys(PIPES, 0)
        for v in nodes:
            result[self.ops[v]["pipe"]] += self.cycles(v)
        return result

    def pressure(self, peak: dict[str, int]) -> int:
        return sum(2 * ceil_div(max(0, peak[p] - capacity), self.settings.bandwidth)
                   for p, capacity in self.settings.capacity.items())

    def scopes(self, num_cores: int) -> list[tuple[str, list[set[int]]]]:
        result = [("global", [set(self.ids)])]
        if len(self.components) <= 1:
            return result
        groups = [set() for _ in range(min(num_cores, len(self.components)))]
        work = [dict.fromkeys(PIPES, 0) for _ in groups]
        weighted = [(c, self.workload(c)) for c in self.components]
        for component, cost in sorted(weighted, key=lambda x: (-max(x[1].values()), min(x[0]))):
            k = min(range(len(groups)), key=lambda k: (
                max(work[k][p] + cost[p] for p in PIPES), sum(work[k].values()), k))
            groups[k].update(component)
            for p in PIPES:
                work[k][p] += cost[p]
        result.append(("components", groups))
        return result
