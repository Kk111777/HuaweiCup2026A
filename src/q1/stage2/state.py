"""第二阶段的状态、确定性顺序与基础工作量；不执行文件 I/O。"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
import hashlib
import heapq
import json
import math

from .. import official
from ..graph import GraphIndex


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def objective(result):
    return result["makespan"], result["data_movement_bytes"]["added_copy_bytes"]


class CycleError(ValueError):
    pass


def topological(preds):
    degree = {v: len(p) for v, p in preds.items()}
    succs = {v: set() for v in preds}
    for v, ps in preds.items():
        for p in ps:
            succs[p].add(v)
    ready = [v for v, d in degree.items() if not d]
    heapq.heapify(ready)
    order = []
    while ready:
        v = heapq.heappop(ready)
        order.append(v)
        for w in sorted(succs[v]):
            degree[w] -= 1
            if not degree[w]:
                heapq.heappush(ready, w)
    if len(order) != len(preds):
        raise CycleError("Task execution constraints contain a cycle")
    return order


def closure(seeds, adjacency, allowed=None):
    seen = set(seeds)
    stack = list(seen)
    while stack:
        v = stack.pop()
        for w in adjacency[v]:
            if w not in seen and (allowed is None or w in allowed):
                seen.add(w)
                stack.append(w)
    return seen


@dataclass(frozen=True)
class SearchOptions:
    search_seconds: float = 1800
    region_seconds: float = 120
    region_evaluations: int = 256
    seeds_per_principle: int = 8
    partners_per_seed: int = 8
    max_region_tasks: int = 3
    max_fragments: int = 6
    ddr_endpoints: int = 12
    layer_shares: tuple = (0.2, 0.3, 0.5)
    pruning: tuple = ("partial", "b0", "b1", "ddr")

    def __post_init__(self):
        for name in ("search_seconds", "region_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        for name in ("region_evaluations", "seeds_per_principle", "partners_per_seed",
                     "max_region_tasks", "max_fragments", "ddr_endpoints"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.ddr_endpoints < 2:
            raise ValueError("ddr_endpoints must be at least two")
        if (len(self.layer_shares) != 3 or any(not isinstance(x, (int, float)) or
                isinstance(x, bool) or not math.isfinite(x) or x <= 0 for x in self.layer_shares)
                or abs(sum(self.layer_shares) - 1) > 1e-9):
            raise ValueError("layer_shares must be three positive fractions summing to one")
        if len(set(self.pruning)) != len(self.pruning) or set(self.pruning) - {"partial", "b0", "b1", "ddr"}:
            raise ValueError("unknown or repeated pruning option")

    def record(self):
        return asdict(self)


class Context:
    def __init__(self, graph, settings):
        self.index = GraphIndex(graph, settings)
        self.graph, self.settings = graph, settings
        self.ids = set(self.index.ids)
        full_preds, _ = official._build_op_adjacency(graph)
        # Official boundary construction preserves these edges, but does NOT
        # necessarily preserve a path through removed COPY nodes inside a Task.
        self.bound_preds = {v: full_preds[v] & self.ids for v in self.ids}
        self.position = {v: i for i, v in enumerate(self.index.order)}
        from multicore_cut_evaluate_problem_1 import _copy_traffic_bytes
        self.original_bytes = _copy_traffic_bytes(graph)
        self._measures = OrderedDict()
        # Per-context only: neither input graphs nor partitions cross this boundary.
        self._partitions = OrderedDict()
        self.total_work = tuple(sum(max(1, op["cycles"]) for op in self.index.ops.values()
                                    if op["pipe"] == pipe) for pipe in ("PIPE_M", "PIPE_V"))
        path = {}
        for v in self.index.order:
            path[v] = max(1, self.index.cycles(v)) + max((path[p] for p in self.bound_preds[v]), default=0)
        self.compute_path = max(path.values(), default=0)

    def measure(self, nodes):
        key = frozenset(nodes)
        if key in self._measures:
            self._measures.move_to_end(key)
            return self._measures[key]
        idx = self.index
        touched = set().union(*(idx.inputs[v] | idx.outputs[v] for v in key)) if key else set()
        ins = {t for t in touched if idx.tensors[t].producer not in key and idx.tensors[t].consumers & key}
        outs = {t for t in touched if idx.tensors[t].producer in key and
                (idx.tensors[t].final_output or idx.tensors[t].consumers - key)}
        work = tuple(sum(max(1, idx.cycles(v)) for v in key if idx.ops[v]["pipe"] == pipe)
                     for pipe in ("PIPE_M", "PIPE_V"))
        path = {}
        for v in sorted(key, key=self.position.__getitem__):
            path[v] = max(1, idx.cycles(v)) + max((path[p] for p in self.bound_preds[v] & key), default=0)
        q = sum(idx.tensors[t].copy_cycles for t in ins) + sum(idx.tensors[t].copy_cycles for t in outs)
        size = sum(idx.tensors[t].size for t in ins) + sum(idx.tensors[t].size for t in outs)
        value = dict(work=work, inputs=ins, outputs=outs, q=q, bytes=size,
                     d0=max(*work, q, max(path.values(), default=0)))
        self._measures[key] = value
        if len(self._measures) > 256:
            self._measures.popitem(last=False)
        return value


class PlanState:
    def __init__(self, context, plan, *, check_cycle=True):
        self.context = context
        if set(plan) != {"node_to_subgraph", "core_schedules"}:
            raise ValueError("plan must contain exactly the two submission fields")
        raw = plan["node_to_subgraph"]
        self.mapping = {int(v): j for v, j in raw.items()}
        if len(raw) != len(self.mapping) or set(self.mapping) != context.ids:
            raise ValueError("plan does not cover every computation exactly once")
        if any(type(j) is not int or j < 0 for j in self.mapping.values()):
            raise ValueError("Task IDs must be nonnegative integers")
        self.orders = [list(order) for order in plan["core_schedules"]]
        if len(self.orders) not in (2, 3, 4, 5):
            raise ValueError("stage 2 requires 2--5 cores")
        flat = [j for order in self.orders for j in order]
        if any(type(j) is not int for j in flat) or len(set(flat)) != len(flat) or set(flat) != set(self.mapping.values()):
            raise ValueError("each Task must be scheduled exactly once")
        # Keep input iteration order in this private key as well as membership.
        # It preserves the original set construction order on a cache miss.
        partition_key = tuple(self.mapping.items())
        partition = context._partitions.get(partition_key)
        if partition is None:
            nodes = {j: set() for j in sorted(flat)}
            for v, j in self.mapping.items():
                nodes[j].add(v)
            preds = {j: set() for j in nodes}
            for v, j in self.mapping.items():
                preds[j].update(self.mapping[p] for p in context.index.preds[v] if self.mapping[p] != j)
            succs = {j: set() for j in nodes}
            for j, ps in preds.items():
                for p in ps:
                    succs[p].add(j)
            canonical = {str(v): self.mapping[v] for v in sorted(self.mapping)}
            encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            partition = dict(nodes=nodes, preds=preds, succs=succs, canonical=canonical,
                             encoded=encoded, signature=hashlib.sha256(encoded).hexdigest())
            context._partitions[partition_key] = partition
            if len(context._partitions) > 2:
                context._partitions.popitem(last=False)
        else:
            context._partitions.move_to_end(partition_key)
        self._partition = partition
        self.partition_signature = partition["signature"]
        # Public state containers remain independent of the cached template.
        self.nodes = {j: ns.copy() for j, ns in partition["nodes"].items()}
        self.core = {j: k for k, order in enumerate(self.orders) for j in order}
        self.preds = {j: ps.copy() for j, ps in partition["preds"].items()}
        self.succs = {j: ss.copy() for j, ss in partition["succs"].items()}
        self.combined = self.with_orders(self.orders)
        self.topo = topological(self.combined) if check_cycle else []
        self.plan = {"node_to_subgraph": partition["canonical"].copy(),
                     "core_schedules": self.orders}
        # Exactly the original canonical JSON bytes, with the large mapping
        # serialized only once per cached partition. No fingerprint migration.
        hasher = hashlib.sha256(b'{"core_schedules":')
        hasher.update(json.dumps(self.orders, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
        hasher.update(b',"node_to_subgraph":')
        hasher.update(partition["encoded"])
        hasher.update(b'}')
        self.signature = hasher.hexdigest()
        self.measure = {j: context.measure(ns) for j, ns in self.nodes.items()}

    def with_orders(self, orders):
        preds = {j: set(ps) for j, ps in self.preds.items()}
        for order in orders:
            for a, b in zip(order, order[1:]):
                preds[b].add(a)
        return preds

    def waits(self):
        cfg = self.context.settings
        waits = {(p, j): cfg.cross_core_wait if self.core[p] != self.core[j] else 0
                 for j, ps in self.preds.items() for p in ps}
        for order in self.orders:
            for p, j in zip(order, order[1:]):
                waits[p, j] = max(waits.get((p, j), 0), cfg.same_core_wait)
        return waits


@dataclass
class OptimizationResult:
    plan: dict
    evaluation: dict
    initial_evaluation: dict
    status: str
    history: list
    records: list
    statistics: dict
