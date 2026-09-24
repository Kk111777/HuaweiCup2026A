"""计算 DAG、张量索引及式 (1)—(5) 的静态量。"""
from __future__ import annotations

from dataclasses import dataclass
import heapq

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
        official.validate_graph(graph)
        self.graph = graph
        self.settings = settings
        all_ops = {op["id"]: op for op in graph["ops"]}
        self.ops = {v: op for v, op in sorted(all_ops.items())
                    if op["op"] not in official.EXCLUDED_COPY_TYPES}
        self.ids = tuple(self.ops)
        for v, op in self.ops.items():
            if op["pipe"] not in PIPES:
                raise ValueError(f"initialization model requires PIPE_M/PIPE_V: op {v}")
        _, full_succs = official._build_op_adjacency(graph)
        self.preds, self.succs = official._contract_excluded_copy_nodes(self.ids, full_succs)
        self.inputs = {v: set() for v in self.ids}
        self.outputs = {v: set() for v in self.ids}
        producers = {t["id"]: set() for t in graph["tensors"]}
        consumers = {t["id"]: set() for t in graph["tensors"]}
        for edge in graph["edges"]:
            a, b = edge["source"], edge["target"]
            if a in all_ops and b in producers:
                producers[b].add(a)
                if a in self.ops:
                    self.outputs[a].add(b)
            elif a in consumers and b in all_ops:
                consumers[a].add(b)
                if b in self.ops:
                    self.inputs[b].add(a)
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
        indegree = {v: len(self.preds[v]) for v in self.ids}
        ready = [v for v in self.ids if indegree[v] == 0]
        heapq.heapify(ready)
        self.order = []
        while ready:
            v = heapq.heappop(ready)
            self.order.append(v)
            for u in sorted(self.succs[v]):
                indegree[u] -= 1
                if indegree[u] == 0:
                    heapq.heappush(ready, u)
        if len(self.order) != len(self.ids):
            raise ValueError("contracted computation graph is cyclic")
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
        self.components = []
        unseen = set(self.ids)
        for root in self.ids:
            if root not in unseen:
                continue
            component, pending = set(), [root]
            unseen.remove(root)
            while pending:
                v = pending.pop()
                component.add(v)
                neighbors = (self.preds[v] | self.succs[v]) & unseen
                unseen.difference_update(neighbors)
                pending.extend(sorted(neighbors, reverse=True))
            self.components.append(component)
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
