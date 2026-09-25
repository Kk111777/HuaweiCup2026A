"""保留原图与张量关联；操作 DAG 仅用于候选生成，不替代官方核内顺序。"""
from __future__ import annotations

from collections import Counter, defaultdict
from functools import cached_property
import heapq
from pathlib import Path
import sys

from .official import validate_graph, _build_op_adjacency, _contract_excluded_copy_nodes


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


def quantile(values, q):
    values = sorted(values)
    return values[round((len(values) - 1) * q)] if values else 0


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

    @cached_property
    def regions(self):
        from .improvement.regions import discover_regions
        return discover_regions(self)

    def touched(self, block):
        return set().union(*(self.inputs[v] | self.outputs[v] for v in block)) if block else set()

    def reference_pressure(self, block):
        """指定参考拓扑序下、无换出时的峰值，仅用于触发搜索，不判不可行。"""
        order = sorted(block, key=self.rank.__getitem__)
        positions = {v: i for i, v in enumerate(order)}
        events = defaultdict(Counter)
        for tid in self.touched(block):
            t = self.tensors[tid]
            pos = "UB" if t["pos"] == "DDR" else t["pos"]
            users = (self.producers[tid] | self.consumers[tid]) & block
            points = [positions[v] for v in users]
            if not points:
                continue
            start, end = min(points), max(points)
            # 对外输出保留至参考序列末端，是有意保守的代理。
            if self.producers[tid] & block and self.consumers[tid] - block:
                end = len(order) - 1
            events[2 * start][pos] += t["size"]
            events[2 * end + 1][pos] -= t["size"]
        live, peak = Counter(), Counter()
        for i in sorted(events):
            live.update(events[i])
            for pos in ("L1", "UB"):
                peak[pos] = max(peak[pos], live[pos])
        return dict(peak)

    def features(self, capacity, near_critical_fraction=.05):
        signatures = Counter()
        for block in self.components:
            signatures[str(sorted(Counter((self.ops[v]["pipe"], len(self.pred[v]),
                                           len(self.succ[v])) for v in block).items()))] += 1
        widths = Counter(self.level.values())
        loads = self.workload(self.ops)
        return {
            "operations": len(self.ops), "original_operations": len(self.all_ops),
            "tensors": len(self.tensors), "weak_components": len(self.components),
            "component_size_histogram": dict(Counter(map(len, self.components))),
            "degree_joint": {str(k): v for k, v in Counter(
                (len(self.pred[v]), len(self.succ[v])) for v in self.ops).items()},
            "chains": len(self.chains), "chain_length_histogram": dict(Counter(map(len, self.chains))),
            "chain_work_q50_q90": [quantile([sum(self.cost[v] for v in c) for c in self.chains], q)
                                    for q in (0.5, 0.9)],
            "levels": max(widths, default=-1) + 1, "level_widths": dict(sorted(widths.items())),
            "pipe_cycles": dict(loads), "critical_path_compute_cycles": self.critical,
            "near_critical_operations": sum(s <= near_critical_fraction * self.critical for s in self.slack.values()),
            "compute_lower_bound_by_cores": {n: max(self.critical, loads["PIPE_M"] / n,
                                                     loads["PIPE_V"] / n) for n in range(1, 6)},
            "tensor_stats": {pos: {
                "size_q90": quantile([t["size"] for t in self.tensors.values() if t["pos"] == pos], .9),
                "large_count": sum(t["size"] >= .1 * capacity[pos] for t in self.tensors.values()
                                   if t["pos"] == pos),
            } for pos in capacity},
            "consumer_count_histogram": dict(Counter(len(c & self.ops.keys()) for c in self.consumers.values())),
            "repeated_component_signatures": signatures.most_common(12),
            "reference_pressure_whole_graph": self.reference_pressure(set(self.ops)),
            "pressure_note": "topological no-spill proxy; not official occupancy or a feasibility test",
        }
