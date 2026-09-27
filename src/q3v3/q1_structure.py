"""Q1v3 structural mechanisms adapted to Q3-owned graph/search interfaces.

Origin: integration/q1-structure-modular-publish/src/q1/improvement/
structural.py (anchor phases) and regions.py (postdominators/opened covers).
Only graph algorithms are reused. There are no Q1 Task barriers, scores,
scheduler imports or dependencies on a live Q1 checkout in this module.
"""
from collections import defaultdict
from statistics import median

from .graph import components


def anchor_phases(graph, pipe, minimum_cycles=0):
    phase = {}
    for v in graph.order:
        phase[v] = max((phase[p] for p in graph.pred[v]), default=0) + int(
            graph.ops[v]['pipe'] == pipe and graph.cost[v] >= minimum_cycles)
    return phase


def discover_regions(graph, check=lambda: None):
    """Fork-to-real-postdominator regions, including true output exits.

    Reverse-DAG LCA uses O(V log V) auxiliary space instead of a quadratic
    reachability matrix. Overlarge covers are skipped, not truncated.
    """
    order, rank = graph.order, graph.rank
    n, height = len(order), max(1, (len(order) + 1).bit_length())
    up = [None] * n + [[n] * height]
    depth, parent = [0] * (n + 1), [n] * n
    terminals = {v for v in order if not graph.succ[v] or any(
        any(graph.all_ops[c]['op'] == 'COPY_OUT' for c in graph.consumers[t])
        for t in graph.outputs[v])}

    def lca(a, b):
        if depth[a] < depth[b]:
            a, b = b, a
        difference = depth[a] - depth[b]
        for k in range(height):
            if difference >> k & 1:
                a = up[a][k]
        if a == b:
            return a
        for k in reversed(range(height)):
            if up[a][k] != up[b][k]:
                a, b = up[a][k], up[b][k]
        return up[a][0]

    for v in reversed(order):
        check()
        successors = [rank[w] for w in sorted(graph.succ[v])]
        if v in terminals:
            successors.append(n)
        p = successors[0]
        for w in successors[1:]:
            p = lca(p, w)
        i = rank[v]
        parent[i], depth[i], up[i] = p, depth[p] + 1, [p]
        for k in range(1, height):
            up[i].append(up[up[i][k - 1]][k - 1])

    regions = set()
    for v in order:
        check()
        p = parent[rank[v]]
        if len(graph.succ[v]) < 2 or p == n:
            continue
        end, members, todo = order[p], {v}, [v]
        for w in todo:
            if w != end:
                for u in sorted(graph.succ[w] - members):
                    members.add(u)
                    todo.append(u)
            if len(members) > 4096:
                break
        else:
            regions.add(frozenset(members))
    return tuple(sorted(regions, key=lambda b: (-len(b), min(b))))


def region_cover(graph, regions, *, opened=False):
    left, outer = set(graph.ops), []
    for region in regions:
        if region <= left:
            outer.append(region)
            left.difference_update(region)
    if opened:
        left, blocks = set(graph.ops), []
        excluded = set(outer)
        for region in regions:
            if region not in excluded and region <= left:
                blocks.append(region)
                left.difference_update(region)
    else:
        blocks = outer
    for chain in graph.chains:
        remaining = chain & left
        if remaining:
            blocks.extend(components(remaining, graph.pred, graph.succ))
    return blocks


class StructureLibrary:
    """Static structure only; trace-driven membership is deliberately uncached."""
    def __init__(self, graph, cores):
        self.graph, self.cores = graph, cores
        work = graph.workload(graph.ops)
        self.pipe = min(work, key=lambda p: (-work[p], p)) if work else 'PIPE_V'
        costs = [graph.cost[v] for v in graph.ops if graph.ops[v]['pipe'] == self.pipe]
        self.phase = anchor_phases(graph, self.pipe)
        self.heavy_phase = anchor_phases(graph, self.pipe, median(costs) if costs else 0)
        self.regions = None

    def layouts(self, scale, check=lambda: None):
        graph = self.graph
        if self.regions is None:
            self.regions = discover_regions(graph, check)
        # Core count controls BOTH phase spans and per-unit work, not just
        # the final number of bins. A larger search radius exposes finer units.
        phases = max(self.phase.values(), default=0)
        span = max(1, (phases + self.cores - 1) // self.cores // (2 ** scale))
        variants = [('anchors', self.phase, span),
                    ('heavy_anchors', self.heavy_phase, max(1, span // 2))]
        seen = set()
        for name, labels, width in variants:
            check()
            bands = defaultdict(set)
            for v in graph.order:
                bands[0 if not labels[v] else 1 + (labels[v] - 1) // width].add(v)
            groups = [b for band in sorted(bands)
                      for b in components(bands[band], graph.pred, graph.succ)]
            signature = frozenset(groups)
            if signature not in seen:
                seen.add(signature)
                yield name, groups
        for opened in (True, False):
            check()
            groups = region_cover(graph, self.regions, opened=opened)
            signature = frozenset(groups)
            if signature not in seen:
                seen.add(signature)
                yield 'open_regions' if opened else 'closed_regions', groups


def bounded_units(graph, groups, cores, scale, hot_ops=()):
    """Split long units at work/L2-consumer boundaries, retaining full coverage.

    These are migration units, not submitted Tasks. Disconnected fragments
    and non-convex covers are safely encoded later as topological priorities.
    """
    limit = max(1, max(graph.workload(graph.ops).values(), default=0) /
                (cores * (2 ** scale)))
    hot_ops, units = set(hot_ops), []
    for group in groups:
        part, loads = [], defaultdict(int)
        for v in sorted(group, key=graph.rank.get):
            pipe, cost = graph.ops[v]['pipe'], graph.cost[v]
            # Hot consumers open a new adjustable interval. A consumer and
            # its following light chain remain together until the next cut.
            if part and (loads[pipe] + cost > limit or v in hot_ops):
                units.extend(components(part, graph.pred, graph.succ))
                part, loads = [], defaultdict(int)
            part.append(v)
            loads[pipe] += cost
        if part:
            units.extend(components(part, graph.pred, graph.succ))
    return units
