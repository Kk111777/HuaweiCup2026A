"""Graph-derived search regions, independent of submitted subgraph boundaries.

Q2-Repair's distinction between logical blocks and priority buckets is used
here. Regions may be non-convex: only their operation/core map is submitted,
using a globally topological sequence. No Q2 runtime or historical score is
imported. All costs below are proposal heuristics, never official scores.
"""
from collections import Counter, defaultdict
import heapq

from .state import PlanState, make_plan


def operation_cores(state):
    return {v: state.core_by_block[s] for v, s in state.owner.items()}


def topological_order(graph, priority=None):
    priority = priority or {v: (-graph.backward[v], graph.rank[v]) for v in graph.ops}
    degree = {v: len(p) for v, p in graph.pred.items()}
    ready = [(priority[v], v) for v, d in degree.items() if not d]
    heapq.heapify(ready)
    order = []
    while ready:
        _, v = heapq.heappop(ready)
        order.append(v)
        for w in sorted(graph.succ[v]):
            degree[w] -= 1
            if not degree[w]:
                heapq.heappush(ready, (priority[w], w))
    return order


def encode_order(graph, core, order, cores, *, encoding='atomic', region_of=None):
    """Atomic buckets preserve intended compute order; runs merge adjacent IDs.

    Runs must be consecutive in the *global* topological order, not merely in
    a core's projection. This prevents introducing a cyclic quotient graph.
    Neither encoding introduces a whole-subgraph execution barrier in Q3.
    """
    if encoding not in ('atomic', 'runs'):
        raise ValueError('unknown region encoding')
    if len(order) != len(graph.ops) or set(order) != set(graph.ops) or set(core) != set(graph.ops):
        raise ValueError('incomplete operation order/core map')
    rank = {v: i for i, v in enumerate(order)}
    if any(rank[u] >= rank[v] for v in order for u in graph.pred[v]):
        raise ValueError('operation order is not topological')
    if any(type(k) is not int or not 0 <= k < cores for k in core.values()):
        raise ValueError('invalid operation core')
    blocks, queues, previous = [], [[] for _ in range(cores)], None
    for v in order:
        identity = (core[v], region_of[v] if region_of else None)
        if encoding == 'atomic' or identity != previous:
            queues[core[v]].append(len(blocks))
            blocks.append([])
        blocks[-1].append(v)
        previous = identity
    plan = make_plan(blocks, queues)
    PlanState(graph, plan)
    return plan


class RegionIndex:
    def __init__(self, graph, groups, settings):
        self.graph = graph
        self.groups = tuple(tuple(sorted(b, key=graph.rank.get)) for b in groups if b)
        flat = [v for b in self.groups for v in b]
        if len(flat) != len(graph.ops) or set(flat) != set(graph.ops):
            raise ValueError('regions must cover operations exactly once')
        self.owner = {v: i for i, b in enumerate(self.groups) for v in b}
        self.work = [graph.workload(b) for b in self.groups]
        self.links = [defaultdict(float) for _ in self.groups]
        bandwidth, delay = settings['bandwidth'], settings['cross_core_copy_delay_cycles']
        for tid, tensor in graph.tensors.items():
            ps = graph.producers[tid] & graph.ops.keys()
            cs = graph.consumers[tid] & graph.ops.keys()
            # Distinct region pairs, not repeated operation edges. This is
            # deliberately only a locality proxy, not an exact COPY count.
            for a in {self.owner[v] for v in ps}:
                for b in {self.owner[v] for v in cs} - {a}:
                    value = delay + 2 * tensor['size'] / bandwidth
                    self.links[a][b] += value
                    self.links[b][a] += value

    def cores(self, assignments):
        return {v: assignments[i] for i, b in enumerate(self.groups) for v in b}

    def majority(self, core):
        return {i: min(set(core[v] for v in b), key=lambda k: (
                    -sum(self.graph.cost[v] for v in b if core[v] == k), k))
                for i, b in enumerate(self.groups)}


def coarsen_regions(graph, cores, settings, beta=2):
    """Heavy dependency-edge agglomeration with a core-dependent work limit.

    High fan-out/in edges are discounted, so a shared stem need not swallow
    all parallel branches. It is safe to join non-convex search regions:
    encode_order retains a topological operation sequence at submission.
    """
    work = graph.workload(graph.ops)
    limit = max(1, max(work.values(), default=0) / (cores * beta))
    parent = {v: v for v in graph.ops}
    loads = {v: Counter({graph.ops[v]['pipe']: graph.cost[v]}) for v in graph.ops}

    def find(v):
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    edge = defaultdict(float)
    delay, bandwidth = settings['cross_core_copy_delay_cycles'], settings['bandwidth']
    for tid, tensor in graph.tensors.items():
        ps = graph.producers[tid] & graph.ops.keys()
        cs = graph.consumers[tid] & graph.ops.keys()
        for u in ps:
            for v in cs:
                branch = max(1, len(graph.succ[u]), len(graph.pred[v]))
                edge[u, v] += (delay + 2 * tensor['size'] / bandwidth) / branch
    for v in graph.order:
        for u in graph.pred[v]:
            edge.setdefault((u, v), delay / max(1, len(graph.succ[u]), len(graph.pred[v])))
    for (u, v), weight in sorted(edge.items(), key=lambda row: (-row[1], *row[0])):
        a, b = find(u), find(v)
        if a == b:
            continue
        merged = loads[a] + loads[b]
        if max(merged.values(), default=0) <= limit:
            # Stable union; operation IDs never index arrays.
            a, b = min(a, b), max(a, b)
            parent[b] = a
            loads[a] = merged
    groups = defaultdict(list)
    for v in graph.order:
        groups[find(v)].append(v)
    return RegionIndex(graph, groups.values(), settings)


def pack_regions(index, cores, affinity=0.0):
    """Joint multi-region core construction using persistent Pipe workloads."""
    assigned, loads = {}, [Counter() for _ in range(cores)]
    ranked = sorted(range(len(index.groups)), key=lambda i: (
        -max(index.work[i].values(), default=0), -sum(index.links[i].values()), i))
    for i in ranked:
        w = index.work[i]
        def key(k):
            local = loads[k] + w
            cut = sum(value for j, value in index.links[i].items()
                      if j in assigned and assigned[j] != k)
            return (max(local.values(), default=0) + affinity * cut,
                    sum(local.values()), k)
        k = min(range(cores), key=key)
        assigned[i] = k
        loads[k].update(w)
    return assigned


def projected_order(graph, state):
    """A legal global order close to an inherited core queue (not its trace)."""
    return topological_order(graph, {v: (state.position[state.owner[v]],
                                       -graph.backward[v], graph.rank[v]) for v in graph.ops})


def locality_order(graph, core, *, memory=False):
    """Ready-operation order balancing critical releases and local lifetimes.

    At most eight critical ready operations compete per step; live bytes are
    a static lifetime heuristic, not Step1's exact COPY/space-reuse schedule.
    """
    local_remaining = Counter((tid, core[v]) for v in graph.ops for tid in graph.inputs[v])
    seen, degree = set(), {v: len(p) for v, p in graph.pred.items()}
    ready = [(-graph.backward[v], v) for v, d in degree.items() if not d]
    heapq.heapify(ready)
    order, last_core = [], None
    while ready:
        sample = [heapq.heappop(ready) for _ in range(min(8, len(ready)))]
        def key(row):
            _, v = row
            k = core[v]
            freed = sum(graph.tensors[t]['size'] for t in graph.inputs[v]
                        if local_remaining[t, k] == 1 and (t, k) in seen)
            opened = sum(graph.tensors[t]['size'] for t in graph.inputs[v] | graph.outputs[v]
                         if (t, k) not in seen)
            return ((opened - freed) if memory else 0, k != last_core,
                    -graph.backward[v], graph.rank[v])
        winner = min(sample, key=key)
        sample.remove(winner)
        for row in sample:
            heapq.heappush(ready, row)
        v = winner[1]
        order.append(v)
        last_core = core[v]
        for tid in graph.inputs[v] | graph.outputs[v]:
            seen.add((tid, core[v]))
        for tid in graph.inputs[v]:
            local_remaining[tid, core[v]] -= 1
        for w in sorted(graph.succ[v]):
            degree[w] -= 1
            if not degree[w]:
                heapq.heappush(ready, (-graph.backward[w], w))
    return order
