"""Scene-B placement proposals from graph structure, never a Q1 score model."""
from __future__ import annotations

from collections import defaultdict
import heapq

from .state import PlanState, make_plan


def refine_blocks(graph, plan, mode='chains'):
    """Expose existing internal priorities without duplicating any operation."""
    old = PlanState(graph, plan)
    blocks, queues = [], [[] for _ in old.queues]
    chain_of = {v: i for i, chain in enumerate(graph.chains) for v in chain}
    for core, queue in enumerate(old.queues):
        for sid in queue:
            groups = defaultdict(list)
            for v in sorted(old.blocks[sid], key=graph.rank.get):
                groups[chain_of[v] if mode == 'chains' else v].append(v)
            # Grouping across an existing boundary can create a quotient cycle;
            # only split, preserve topological group order, then validate.
            for group in sorted(groups.values(), key=lambda b: min(graph.rank[v] for v in b)):
                queues[core].append(len(blocks))
                blocks.append(group)
    candidate = make_plan(blocks, queues)
    PlanState(graph, candidate)
    return candidate


def place_blocks(graph, plan, settings, *, communication_weight=1.0, priority='critical'):
    """An operation/Pipe list heuristic used only to propose a full plan.

    It estimates releases from the actual producer operation, not the end of
    an entire Q1 Task. Official compilation, spill, bandwidth sharing and FIFO
    simulation still decide acceptance; this estimate is not a reported score.
    """
    state = PlanState(graph, plan)
    n = len(state.queues)
    pipe_free = [defaultdict(float) for _ in range(n)]
    op_end, op_core = {}, {}
    edge_bytes = defaultdict(int)
    for tid, tensor in graph.tensors.items():
        producers = graph.producers[tid] & graph.ops.keys()
        consumers = graph.consumers[tid] & graph.ops.keys()
        for u in producers:
            for v in consumers:
                edge_bytes[u, v] += tensor['size']
    degree = {s: len(p) for s, p in state.pred.items()}
    ordered_ops = {s: sorted(b, key=graph.rank.get) for s, b in state.blocks.items()}
    critical = {s: max(graph.backward[v] for v in b) for s, b in state.blocks.items()}
    def key(s):
        if priority == 'level':
            return min(graph.level[v] for v in state.blocks[s]), -critical[s], s
        return -critical[s], min(graph.rank[v] for v in state.blocks[s]), s
    ready = [(key(s), s) for s, d in degree.items() if not d]
    heapq.heapify(ready)
    queues = [[] for _ in range(n)]
    delay = settings['cross_core_copy_delay_cycles']
    bandwidth = settings['bandwidth']
    while ready:
        _, sid = heapq.heappop(ready)
        options = []
        for core in range(n):
            pipes, ends = dict(pipe_free[core]), {}
            communication = 0
            for v in ordered_ops[sid]:
                release = 0.
                for u in graph.pred[v]:
                    finish = ends[u] if u in ends else op_end[u]
                    if u not in ends and op_core[u] != core:
                        traffic = 2 * edge_bytes[u, v]
                        communication += traffic
                        finish += communication_weight * (delay + traffic / bandwidth)
                    release = max(release, finish)
                pipe = graph.ops[v].get('pipe', 'PIPE_V')
                end = max(release, pipes.get(pipe, 0)) + graph.cost[v]
                pipes[pipe] = ends[v] = end
            local_finish = max(ends.values(), default=0)
            global_finish = max([local_finish] + [max(p.values(), default=0) for p in pipe_free])
            options.append(((global_finish, local_finish, communication, len(queues[core]), core),
                            core, pipes, ends))
        _, core, pipes, ends = min(options, key=lambda row: row[0])
        queues[core].append(sid)
        pipe_free[core] = defaultdict(float, pipes)
        op_end.update(ends)
        op_core.update({v: core for v in ends})
        for target in sorted(state.succ[sid]):
            degree[target] -= 1
            if not degree[target]:
                heapq.heappush(ready, (key(target), target))
    return state.with_queues(queues)


def construction_candidates(graph, plan, settings):
    """Bounded families; finer control need not imply more cross-core cuts."""
    variants = [('inherited', plan)]
    for mode in ('chains', 'operations'):
        try:
            refined = refine_blocks(graph, plan, mode)
        except ValueError:
            continue
        if PlanState(graph, refined).digest != PlanState(graph, plan).digest:
            variants.append((mode, refined))
            yield f'priority_resolution_{mode}', refined
    # Cover chain-aware parallel placement before spending all variants on a
    # coarse inherited partition. This is essential under a short wall budget.
    variants.sort(key=lambda row: {'chains': 0, 'inherited': 1, 'operations': 2}[row[0]])
    for name, source in variants:
        for weight in (.5, 1., 2.):
            yield f'placement_{name}_comm{weight:g}', place_blocks(
                graph, source, settings, communication_weight=weight)
        yield f'placement_{name}_level', place_blocks(graph, source, settings, priority='level')
