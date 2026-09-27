"""Q3-owned operation DAG and tensor index; independent of Q1 package layout.

The graph-only index preserves the previous Q3 structural behavior. Official
validation and COPY contraction remain authoritative; no Q1 score or scheduler
is imported. The Q1 refactor is free to move its own model independently.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import heapq
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "official" / "code"))
from evaluation_validation import validate_graph
from stub_multicore_cut_and_schedule import (
    _build_op_adjacency, _contract_excluded_copy_nodes,
)


def topological(preds, succs):
    degrees = {v: len(p) for v, p in preds.items()}
    ready = [v for v, d in degrees.items() if d == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        v = heapq.heappop(ready)
        order.append(v)
        for w in sorted(succs[v]):
            degrees[w] -= 1
            if not degrees[w]:
                heapq.heappush(ready, w)
    if len(order) != len(degrees):
        raise ValueError("dependency cycle")
    return order


def components(nodes, preds, succs):
    left = set(nodes)
    result = []
    for start in sorted(left):
        if start not in left:
            continue
        left.remove(start)
        queue = [start]
        for v in queue:
            for w in sorted((preds[v] | succs[v]) & left):
                left.remove(w)
                queue.append(w)
        result.append(frozenset(queue))
    return result


class Graph:
    def __init__(self, raw):
        validate_graph(raw)
        self.raw = raw
        self.all_ops = {o["id"]: o for o in raw["ops"]}
        self.ops = {i: o for i, o in self.all_ops.items()
                    if o["op"] not in {"COPY_IN", "COPY_OUT"}}
        _, full_succs = _build_op_adjacency(raw)
        self.pred, self.succ = _contract_excluded_copy_nodes(sorted(self.ops), full_succs)
        self.order = topological(self.pred, self.succ)
        self.rank = {v: i for i, v in enumerate(self.order)}
        self.tensors = {t["id"]: t for t in raw["tensors"]}
        self.producers, self.consumers = defaultdict(set), defaultdict(set)
        self.inputs, self.outputs = defaultdict(set), defaultdict(set)
        for edge in raw["edges"]:
            a, b = edge["source"], edge["target"]
            if a in self.all_ops and b in self.tensors:
                self.producers[b].add(a)
                self.outputs[a].add(b)
            elif a in self.tensors and b in self.all_ops:
                self.consumers[a].add(b)
                self.inputs[b].add(a)
        self.level, self.forward, self.backward = {}, {}, {}
        # 与官方零周期操作至少占 1 cycle 的语义一致；COPY 另由官方处理。
        self.cost = {v: max(1, o.get("cycles", 1)) for v, o in self.ops.items()}
        for v in self.order:
            self.level[v] = max((self.level[u] + 1 for u in self.pred[v]), default=0)
            self.forward[v] = self.cost[v] + max((self.forward[u] for u in self.pred[v]), default=0)
        for v in reversed(self.order):
            self.backward[v] = self.cost[v] + max((self.backward[u] for u in self.succ[v]), default=0)
        self.critical = max(self.forward.values(), default=0)
        self.slack = {v: self.critical - self.forward[v] - self.backward[v] + self.cost[v]
                      for v in self.ops}
        self.components = components(self.ops, self.pred, self.succ)
        self.chains = []
        visited = set()
        for v in self.order:
            if v in visited:
                continue
            chain = [v]
            visited.add(v)
            while len(self.succ[chain[-1]]) == 1:
                w = next(iter(self.succ[chain[-1]]))
                if len(self.pred[w]) != 1 or w in visited:
                    break
                chain.append(w)
                visited.add(w)
            self.chains.append(frozenset(chain))

    def workload(self, block):
        loads = Counter()
        for v in block:
            loads[self.ops[v]["pipe"]] += self.cost[v]
        return loads
