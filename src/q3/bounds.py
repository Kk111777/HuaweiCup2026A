"""Candidate-specific optimistic bounds; fractional bandwidth uses exact rationals.

Adapted from the supplied Q3 mechanism prototype (2026-09-26). Bounds only
discard candidates unable to improve the lexicographic official objective.
"""
from collections import defaultdict
from fractions import Fraction
import heapq
import math


def bandwidth_lower_bound(fixed, groups):
    total = fixed + sum(m * d for m, d, _ in groups)
    cursor = saved = Fraction(0)
    for m, d, l in sorted((g for g in groups if g[0] > 1),
                          key=lambda g: Fraction(g[1], g[2]), reverse=True):
        span, ratio = Fraction((m - 1) * l), Fraction(d, l)
        root = (total - saved + cursor * ratio) / (1 + ratio)
        if root <= cursor + span:
            return max(cursor, root)
        cursor += span
        saved += (m - 1) * d
    return Fraction(total) - saved


def lower_bounds(bundle, settings):
    from schedule_step3 import _op_duration, _uses_ddr_bandwidth
    tasks, links, _, _, _ = bundle
    pred, duration, pipe_sums = {}, {}, []
    grouped, bad_keys, fixed = defaultdict(list), set(), 0
    ddr, l2, capacity = settings.scene.bandwidth, settings.cache_bandwidth, settings.cache_capacity
    for core, task in tasks.items():
        for oid, op in task['op_by_id'].items():
            node = core, oid
            pred[node] = {(core, u): 0 for u in task['op_preds'][oid]}
            args = op, task['in_tids'], task['out_tids'], task['tensor_by_id']
            d = _op_duration(*args, ddr)
            local = [t for t in task['out_tids'][oid] if task['tensor_by_id'][t]['pos'] != 'DDR'] if op['op'] == 'COPY_IN' else []
            eligible = (len(local) == 1 and len(task['out_tids'][oid]) == 1
                        and task['tensor_by_id'][local[0]]['size'] > 0
                        and _uses_ddr_bandwidth(*args))
            if op['op'] == 'COPY_IN' and local and not eligible:
                tensor = task['tensor_by_id'][local[0]]
                bad_keys.add(tensor.get('logical_tid', local[0]))
            if eligible:
                tensor = task['tensor_by_id'][local[0]]
                l = _op_duration(*args, l2)
                grouped[tensor.get('logical_tid', local[0])].append((tensor['size'], d, l))
                duration[node] = min(d, l)
            elif op['op'] == 'COPY_IN':
                duration[node] = min(d, _op_duration(*args, l2))
            else:
                duration[node] = d
                if _uses_ddr_bandwidth(*args):
                    fixed += d
        for order in task['pipe_ops'].values():
            pipe_sums.append(sum(duration[core, v] for v in order))
            for a, b in zip(order, order[1:]):
                pred[core, b][core, a] = max(pred[core, b].get((core, a), 0), 0)
    for link in links:
        a = link['source_core'], link['source_copy_out_id']
        b = link['target_core'], link['target_copy_in_id']
        pred[b][a] = max(pred[b].get(a, 0), settings.scene.delay)
    successors = {v: [] for v in pred}
    indegree = {v: len(ps) for v, ps in pred.items()}
    for v, ps in pred.items():
        for u in ps:
            successors[u].append(v)
    ready = [v for v, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    ends = {}
    while ready:
        v = heapq.heappop(ready)
        ends[v] = duration[v] + max((ends[u] + lag for u, lag in pred[v].items()), default=0)
        for w in successors[v]:
            indegree[w] -= 1
            if not indegree[w]:
                heapq.heappush(ready, w)
    if len(ends) != len(pred):
        raise ValueError('execution cycle in lower-bound input')
    groups = []
    for key, rows in grouped.items():
        if key in bad_keys or len(set(rows)) != 1:
            continue  # nonstandard logical keys weaken, never strengthen, this bound
        size, d, l = rows[0]
        if size > capacity:
            fixed += len(rows) * d
        else:
            groups.append((len(rows), d, l))
    bw = bandwidth_lower_bound(fixed, groups)
    parts = dict(path=max(ends.values(), default=0), pipe=max(pipe_sums, default=0),
                 bandwidth_numerator=bw.numerator, bandwidth_denominator=bw.denominator)
    return max(parts['path'], parts['pipe'], math.ceil(bw)), parts


def cannot_improve(lower, added, incumbent):
    return incumbent is not None and (lower > incumbent[0] or
                                      (lower == incumbent[0] and added >= incumbent[1]))
