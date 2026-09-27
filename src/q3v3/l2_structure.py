"""Q3 feedback changes Q1v3-style units, scope, core mapping and priorities.

All measured data belongs to the exact parent plan. Only original Op/Tensor
IDs cross into a proposal; generated COPY IDs/times are never transplanted.
Affinities rank proposals, never replace official compilation/simulation.
"""
from collections import Counter, defaultdict

from .joint_search import refine_assignment
from .neighborhoods import Candidate, _timeline
from .q1_structure import StructureLibrary, bounded_units
from .regions import RegionIndex, encode_order, operation_cores, topological_order
from .state import PlanState


class Feedback:
    def __init__(self, graph, state, result, settings, *, enable_l2=True):
        self.graph, self.settings = graph, settings
        self.state_digest, self.enable_l2 = state.digest, enable_l2
        self.core = operation_cores(state)
        self.timeline = _timeline(result)
        self.reads, self.pressure, self.cache_pressure = defaultdict(list), Counter(), Counter()
        self.hit_probability, self.reuse_bytes = {}, {}
        self.starts, self.ends = {}, {}
        total = max(1, result['makespan'])
        for v, k in self.core.items():
            op = self.timeline.get((k, v), {})
            self.starts[v], self.ends[v] = op.get('start', 0), op.get('end', 0)
            # A late, large compute interval can be a load bottleneck even
            # when no cache reuse exists. This is a ranking, not exact slack.
            self.pressure[v] = graph.cost[v] * (self.ends[v] / total) ** 2
        for event in result.get('cache_events', ()) if enable_l2 else ():
            if event['event'] in ('hit', 'miss') and event.get('tensor_id') in graph.tensors:
                self.reads[event['tensor_id']].append(event)
        capacity, bandwidth = settings['cache_capacity_bytes'], settings['bandwidth']
        l2_bandwidth = settings['cache_bandwidth_bytes_per_cycle']
        for tid, reads in self.reads.items():
            size = graph.tensors[tid]['size']
            cacheable = 0 < size <= capacity
            hits = sum(e['event'] == 'hit' for e in reads)
            misses = len(reads) - hits
            self.hit_probability[tid] = min(1., hits / max(1, len(reads)-1)) if cacheable else 0.
            avoidable = max(0, misses-1) * size if cacheable else 0
            self.reuse_bytes[tid] = avoidable
            consumers = graph.consumers[tid] & graph.ops.keys()
            value = avoidable * max(0., 1/bandwidth - 1/l2_bandwidth)
            for v in consumers:
                self.cache_pressure[v] += value / max(1, len(consumers))
                self.pressure[v] += value / max(1, len(consumers))
        # Even a cache hit still requires the source COPY_OUT and the fixed
        # cross-core release delay. Preserve both in structural selection.
        for transfer in result.get('cross_core_transfers', ()):
            tid = transfer['tensor_id']
            if tid not in graph.tensors:
                continue
            source = graph.producers[tid] & graph.ops.keys()
            targets = {v for v in graph.consumers[tid] & graph.ops.keys()
                       if self.core[v] == transfer['target_core']}
            value = settings['cross_core_copy_delay_cycles'] + transfer['size']/bandwidth
            for v in source | targets:
                self.pressure[v] += value / max(1, len(source | targets))

    def summary(self):
        return dict(parent_digest=self.state_digest, cache_feedback=self.enable_l2,
            observed_logical_tensors=len(self.reads),
            repeated_miss_tensors=sum(v > 0 for v in self.reuse_bytes.values()),
            optimistic_avoidable_ddr_bytes=sum(self.reuse_bytes.values()),
            hot_consumers=[v for v, p in self.cache_pressure.most_common(24) if p > 0],
            note='Observed miss bytes after the first miss are an optimistic reuse bound, not a promised saving.')

    def links(self, index):
        """Recalibrate dependency links; also represent producerless shared inputs.

        A sparse consumer chain bounds high-fanout hyperedge cost/space.
        Already successful L2 sharing reduces the incentive to serialize it
        on one core; repeated misses strengthen consumer regrouping instead.
        """
        graph, settings = self.graph, self.settings
        index.links = [defaultdict(float) for _ in index.groups]
        ddr, l2 = settings['bandwidth'], settings['cache_bandwidth_bytes_per_cycle']
        delay = settings['cross_core_copy_delay_cycles']

        def link(a, b, value):
            if a != b:
                index.links[a][b] += value
                index.links[b][a] += value

        for tid, tensor in graph.tensors.items():
            producers = {index.owner[v] for v in graph.producers[tid] & graph.ops.keys()}
            consumers = sorted({index.owner[v] for v in graph.consumers[tid] & graph.ops.keys()})
            probability = self.hit_probability.get(tid, 0.)
            read = tensor['size'] * ((1-probability)/ddr + probability/l2)
            for a in producers:
                for b in consumers:
                    link(a, b, delay + tensor['size']/ddr + read)
            if self.enable_l2 and len(consumers) > 1:
                # Saving another per-core read can matter for raw shared input
                # tensors too; they have no compute-producer DAG edge.
                failed_reuse = self.reuse_bytes.get(tid, 0) / ddr
                value = (read + failed_reuse / len(consumers)) / max(1, len(consumers)-1)
                for a, b in zip(consumers, consumers[1:]):
                    link(a, b, value)


def selected_scope(graph, groups, feedback, cores, scale):
    """Choose several whole migration units and widen across stagnation levels."""
    if scale >= 2:
        return set(range(len(groups)))
    owner = {v:i for i, b in enumerate(groups) for v in b}
    ranked = sorted(range(len(groups)), key=lambda i: (
        -sum(feedback.pressure[v] for v in groups[i]), min(groups[i])))
    limit = min(len(groups), max(4, 2*cores) * (2 ** scale))
    selected = set(ranked[:max(1, limit//2)])
    # Include opposite endpoints, not only the most expensive half of a cut.
    neighbors = {owner[w] for i in selected for v in groups[i]
                 for w in graph.pred[v] | graph.succ[v]} - selected
    for i in sorted(neighbors, key=lambda i: (-sum(feedback.pressure[v] for v in groups[i]), i)):
        if len(selected) >= limit:
            break
        selected.add(i)
    for i in ranked:
        if len(selected) >= limit:
            break
        selected.add(i)
    return selected


def rebuild_index(graph, groups, selected, feedback, settings):
    # Outside the selected region, retain the exact original operation/core
    # map, even when a static graph unit straddles several inherited cores.
    selected_groups = [b for i, b in enumerate(groups) if i in selected]
    fixed = []
    for i, group in enumerate(groups):
        if i in selected:
            continue
        by_core = defaultdict(set)
        for v in group:
            by_core[feedback.core[v]].add(v)
        fixed.extend(frozenset(by_core[k]) for k in sorted(by_core))
    index = RegionIndex(graph, selected_groups + fixed, settings)
    feedback.links(index)
    return index, set(range(len(selected_groups)))


def repack(index, initial, movable, cores):
    """Jointly redistribute a whole region while preserving its outside loads."""
    assignment = {i:k for i,k in initial.items() if i not in movable}
    loads = [Counter() for _ in range(cores)]
    for i,k in assignment.items():
        loads[k].update(index.work[i])
    for i in sorted(movable, key=lambda j: (-max(index.work[j].values(), default=0), j)):
        def score(k):
            updated = loads[k] + index.work[i]
            peak = max(max(updated.values(), default=0), max((max(row.values(), default=0)
                       for c,row in enumerate(loads) if c != k), default=0))
            cut = sum(value for j,value in index.links[i].items() if j in assignment and assignment[j] != k)
            return peak + .25*cut/cores, sum(updated.values()), k
        k = min(range(cores), key=score)
        assignment[i] = k
        loads[k].update(index.work[i])
    return assignment


def feedback_order(graph, library, feedback):
    """Phase-consistent consumer windows, without inventing phase barriers.

    Ready consumers of a repeatedly missed tensor share an ordering label.
    It is only a priority hint; parallel issue, FIFO residence and contention
    will be recomputed, so no hit is inferred from this ordering alone.
    """
    hot = sorted((t for t,b in feedback.reuse_bytes.items() if b),
                 key=lambda t: (-feedback.reuse_bytes[t], t))[:32]
    rank = {tid:i for i,tid in enumerate(hot)}
    label = {v:min((rank[t] for t in graph.inputs[v] if t in rank), default=len(hot))
             for v in graph.ops}
    return topological_order(graph, {v:(library.phase[v], label[v], -graph.backward[v], graph.rank[v])
                                    for v in graph.ops})


def structural_candidates(graph, plan, result, settings, library=None, scale=0,
                          check=lambda: None, *, enable_l2=True):
    """Rebuild several old buckets at once; all complete proposals are replayed."""
    state = PlanState(graph, plan)
    cores = len(state.queues)
    library = library or StructureLibrary(graph, cores)
    feedback = Feedback(graph, state, result, settings, enable_l2=enable_l2)
    seen = {state.digest}
    hot_ops = [v for v,p in feedback.cache_pressure.most_common(4*cores*(scale+1)) if p > 0]
    trace_order = topological_order(graph, {v:(feedback.starts[v], feedback.ends[v], graph.rank[v])
                                           for v in graph.ops})
    window_order = feedback_order(graph, library, feedback)
    for layout, groups in library.layouts(scale, check):
        check()
        units = bounded_units(graph, groups, cores, scale, hot_ops)
        selected = selected_scope(graph, units, feedback, cores, scale)
        index, movable = rebuild_index(graph, units, selected, feedback, settings)
        inherited = index.majority(feedback.core)
        variants = [('repair', inherited, trace_order),
                    ('repack', repack(index, inherited, movable, cores), window_order)]
        for mode, start, order in variants:
            check()
            assignment = refine_assignment(index, start, cores, .25, check, movable=movable)
            core = index.cores(assignment)
            proposed = encode_order(graph, core, order, cores)
            digest = PlanState(graph, proposed).digest
            if digest in seen:
                continue
            seen.add(digest)
            moved = sum(core[v] != feedback.core[v] for v in graph.ops)
            yield Candidate(proposed, f'structural_{layout}_{mode}',
                f'Q1v3-derived cover; radius={scale}, units={len(index.groups)}, '
                f'movable_units={len(movable)}, moved_operations={moved}, '
                f'observed_L2_cut_points={len(hot_ops)}, feedback_parent={state.digest}.')
