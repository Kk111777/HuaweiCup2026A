"""Complete search states; partition, core placement and order all matter."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import heapq
import json

from .graph import Graph, topological


class PlanState:
    def __init__(self, graph, plan):
        self.graph = graph
        if not isinstance(plan, dict) or set(plan) != {'node_to_subgraph', 'core_schedules'}:
            raise ValueError('plan requires exactly node_to_subgraph and core_schedules')
        mapping, schedules = plan['node_to_subgraph'], plan['core_schedules']
        if not isinstance(mapping, dict) or not isinstance(schedules, list):
            raise ValueError('invalid mapping or core schedules')
        normalized = {}
        for key, value in mapping.items():
            if isinstance(key, bool) or not isinstance(key, (str, int)):
                raise ValueError('invalid operation id')
            try:
                op_id = int(key)
            except (ValueError, TypeError) as exc:
                raise ValueError('invalid operation id') from exc
            if str(op_id) in normalized:
                raise ValueError('duplicate operation id after normalization')
            if type(value) is not int or value < 0:
                raise ValueError('subgraph id must be a nonnegative integer')
            normalized[str(op_id)] = value
        if any(not isinstance(q, list) or any(type(s) is not int or s < 0 for s in q) for q in schedules):
            raise ValueError('core schedules require nonnegative integer subgraph ids')
        self.plan = {"node_to_subgraph": normalized,
                     "core_schedules": [list(q) for q in schedules]}
        self.owner = {int(k): v for k, v in self.plan["node_to_subgraph"].items()}
        if set(self.owner) != set(graph.ops):
            raise ValueError("plan must cover every original non-boundary-copy operation exactly")
        self.blocks = defaultdict(set)
        for op, sid in self.owner.items():
            self.blocks[sid].add(op)
        self.blocks = {s: frozenset(v) for s, v in self.blocks.items()}
        self.queues = self.plan["core_schedules"]
        scheduled = [s for q in self.queues for s in q]
        if not self.queues or len(scheduled) != len(set(scheduled)) or set(scheduled) != set(self.blocks):
            raise ValueError("every subgraph must occur once in the core schedules")
        self.core_by_block = {s: c for c, q in enumerate(self.queues) for s in q}
        self.position = {s: j for q in self.queues for j, s in enumerate(q)}
        self.pred = {s: set() for s in self.blocks}
        self.succ = {s: set() for s in self.blocks}
        for v in graph.order:
            for u in graph.pred[v]:
                a, b = self.owner[u], self.owner[v]
                if a != b:
                    self.pred[b].add(a)
                    self.succ[a].add(b)
        self.order = topological(self.pred, self.succ)
        for b, predecessors in self.pred.items():
            for a in predecessors:
                if self.core_by_block[a] == self.core_by_block[b] and self.position[a] >= self.position[b]:
                    raise ValueError("same-core subgraph dependency reversed")
        # This is a cheap structural gate. Only the official operation/Pipe
        # execution validator may decide the legality of the compiled schedule.

    @property
    def digest(self):
        # The official builder traverses mapping insertion order. Labels and
        # order can affect COPY ownership/tie breaks, so only identical full
        # input contracts may share an evaluation identity without compilation.
        payload = [list(self.plan['node_to_subgraph'].items()), self.queues]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()

    @property
    def structural_signature(self):
        # Useful for diversity diagnostics, NOT for skipping official calls.
        payload = [[sorted(self.blocks[s]) for s in q] for q in self.queues]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()

    def with_queues(self, queues):
        return PlanState(self.graph, {"node_to_subgraph": self.plan["node_to_subgraph"],
                                     "core_schedules": queues}).plan

    def rebuild(self, assignments=None, priority=None):
        """Construct one global topological priority, then project onto cores."""
        assignments = self.core_by_block if assignments is None else assignments
        priority = priority or {s: self.position[s] for s in self.blocks}
        degree = {s: len(p) for s, p in self.pred.items()}
        ready = [(priority[s], s) for s, d in degree.items() if not d]
        heapq.heapify(ready)
        queues = [[] for _ in self.queues]
        while ready:
            _, s = heapq.heappop(ready)
            queues[assignments[s]].append(s)
            for t in sorted(self.succ[s]):
                degree[t] -= 1
                if not degree[t]:
                    heapq.heappush(ready, (priority[t], t))
        return self.with_queues(queues)


def make_plan(blocks, queues):
    return {"node_to_subgraph": {str(v): s for s, b in enumerate(blocks) for v in sorted(b)},
            "core_schedules": queues}


def compute_bounds(graph, cores, plan=None):
    """Compute-only lower bounds, independent of measured single-core scores."""
    if type(cores) is not int or cores < 1:
        raise ValueError('cores must be a positive integer')
    costs = {v: (0 if str(o['op']).startswith('COPY') else max(1, o.get('cycles', 1)))
             for v, o in graph.ops.items()}
    earliest, work = {}, defaultdict(int)
    for v in graph.order:
        earliest[v] = costs[v] + max((earliest[u] for u in graph.pred[v]), default=0)
        work[graph.ops[v].get('pipe', 'PIPE_V')] += costs[v]
    cp = max(earliest.values(), default=0)
    resource = max(((v + cores - 1) // cores for v in work.values()), default=0)
    result = dict(compute_critical_path=cp, resource_lower_bound=resource,
                  lower_bound=max(cp, resource), total_compute_by_pipe=dict(work))
    if plan is not None:
        state = PlanState(graph, plan)
        if len(state.queues) != cores:
            raise ValueError('plan core count does not match lower-bound request')
        by_core = [defaultdict(int) for _ in range(cores)]
        for v in graph.ops:
            by_core[state.core_by_block[state.owner[v]]][graph.ops[v].get('pipe', 'PIPE_V')] += costs[v]
        result['per_core_compute_by_pipe'] = [dict(v) for v in by_core]
        result['placement_lower_bound'] = max([cp] + [t for row in by_core for t in row.values()])
    return result
