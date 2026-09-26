"""Source-only proposal families; no case names or historical scores are inputs.

The three primary rules follow the supplied optimized-model prototype. Sparse
updates preserve its priorities; rejected moves never alter their parent.
"""
from collections import defaultdict
from dataclasses import replace
import heapq
import json
import math

from q2.construct import State, try_place, commit, close
from q2.contracts import CandidateOrigin, EncodedCandidate
from q2.encoding import encode_atomic
from q2.state import plan_digest, derive_state, candidate_from_state
from .contracts import Proposal

ORDER_PARAMETERS = tuple({'batch': n} for n in (1, 2, 4, 8, 16, 32)) + tuple(
    dict(release_weight=w, continuation=c)
    for w, c in ((0, 0), (.5, 0), (2, 0), (8, 0), (0, .02), (0, .1), (.5, .1)))
REBUILD_PARAMETERS = tuple({'batch': n} for n in (1, 4, 16)) + tuple(
    dict(release_weight=w, continuation=c) for w, c in ((0, 0), (2, 0), (.5, .1)))


class IncompatibleComputeOrder(ValueError):
    """No serial interleaving for this operator; parent execution may be valid."""


def computation_order(g, state):
    pred, succ = [set(x) for x in g.preds], [set(x) for x in g.succs]
    for row in state.compute_orders_by_core:
        for a, b in zip(row, row[1:]):
            pred[b].add(a); succ[a].add(b)
    degree = list(map(len, pred))
    ready = [v for v, count in enumerate(degree) if not count]
    heapq.heapify(ready)
    order = []
    while ready:
        v = heapq.heappop(ready); order.append(v)
        for w in sorted(succ[v]):
            degree[w] -= 1
            if not degree[w]: heapq.heappush(ready, w)
    if len(order) != len(g.ids):
        raise IncompatibleComputeOrder('actual computation projections have no common serial interleaving')
    return tuple(order)


def build_order(g, core, *, batch=None, release_weight=0., continuation=0., check=lambda: None):
    degree = list(map(len, g.preds))
    ready = {v for v, count in enumerate(degree) if not count}
    consumers = [len(t.consumers) for t in g.tensors]
    last = [None] * (max(core, default=0) + 1)
    bands = [0] * len(g.ids)
    if batch is not None:
        groups = defaultdict(list)
        for component in g.components:
            groups[min(core[v] for v in component)].append(component)
        for components in groups.values():
            for i, component in enumerate(components):
                for v in component: bands[v] = i // batch
    scale = max(g.b, default=1)
    new = [sum(g.tensors[t].size for t in g.outputs[v] if g.tensors[t].consumers)
           for v in range(len(g.ids))]
    versions = [0] * len(g.ids)

    def key(v):
        freed = sum(g.tensors[t].size for t in g.inputs[v] if consumers[t] == 1)
        follow = last[core[v]] is not None and last[core[v]] in g.preds[v]
        priority = g.b[v] + release_weight * (freed - new[v]) / g.settings.bandwidth + continuation * scale * follow
        return bands[v], -priority, g.ids[v], v, versions[v]

    heap = [key(v) for v in ready]
    heapq.heapify(heap)
    order = []
    while heap:
        if len(order) % 128 == 0: check()
        _, _, _, v, version = heapq.heappop(heap)
        if v not in ready or versions[v] != version: continue
        ready.remove(v); order.append(v)
        old = last[core[v]]; last[core[v]] = v
        affected = set()
        for t in g.inputs[v]:
            consumers[t] -= 1
            if release_weight and consumers[t] == 1:
                affected.update(w for w in g.tensors[t].consumers if w in ready)
        if continuation:
            for source in (old, v):
                if source is not None:
                    affected.update(w for w in g.succs[source] if w in ready and core[w] == core[v])
        for w in g.succs[v]:
            degree[w] -= 1
            if not degree[w]:
                ready.add(w); affected.add(w)
        for w in sorted(affected):
            versions[w] += 1; heapq.heappush(heap, key(w))
    if len(order) != len(g.ids): raise ValueError('incomplete ready order')
    return tuple(order)


def atomic_proposal(g, parent, core, order, family, parameters, region=()):
    plan = encode_atomic(g, len(parent.compute_orders_by_core), core, order)
    candidate = EncodedCandidate(json.dumps(plan), None, family, tuple(order), parent.state_signature)
    return Proposal(candidate, family, parent.state_signature, tuple(region), json.dumps(parameters, sort_keys=True))


def order_proposals(g, parent, *, check=lambda: None):
    seen = {parent.plan_digest}
    for parameters in ORDER_PARAMETERS:
        check()
        if 'batch' in parameters and len(g.components) <= 1: continue
        order = build_order(g, parent.core_of_op, check=check, **parameters)
        proposal = atomic_proposal(g, parent, parent.core_of_op, order, 'order', parameters)
        key = plan_digest(proposal.candidate.plan)
        if key not in seen:
            seen.add(key); yield proposal


def eft_rebuild(g, cores, order, weight=0, *, check=lambda: None):
    state = State(g, cores)
    for i, v in enumerate(order):
        if i % 64 == 0: check()
        trials = [try_place(g, state, v, k) for k in range(cores)]
        trial = min(trials, key=lambda t: (t.end + weight * t.added_work, t.added_bytes,
                                          max(state.work[t.core]), t.core))
        commit(g, state, trial, seed=True, limit=max(1, g.duration[v]))
        close(state)
    return tuple(state.core)


def rebuild_proposals(g, parent, *, check=lambda: None):
    seen = {parent.plan_digest}
    for parameters in REBUILD_PARAMETERS:
        if 'batch' in parameters and len(g.components) <= 1: continue
        order = build_order(g, parent.core_of_op, check=check, **parameters)
        for weight in (0, 1):
            check()
            core = eft_rebuild(g, len(parent.compute_orders_by_core), order, weight, check=check)
            proposal = atomic_proposal(g, parent, core, order, 'rebuild', dict(parameters, copy_weight=weight))
            key = plan_digest(proposal.candidate.plan)
            if key not in seen:
                seen.add(key); yield proposal


class TrafficIndex:
    """Exact consumer-core multiplicities for source/consumer migration deltas."""
    def __init__(self, g, core, cores):
        self.g, self.core = g, tuple(core)
        self.loads = [[0, 0] for _ in range(cores)]
        for v, k in enumerate(core): self.loads[k][g.pipe[v]] += g.duration[v]
        self.counts, self.cuts = [], 0
        for tensor in g.tensors:
            counts = defaultdict(int)
            for v in tensor.consumers: counts[core[v]] += 1
            self.counts.append(dict(counts))
            if tensor.producer is not None: self.cuts += len(set(counts) - {core[tensor.producer]})
        self.work, self.bytes = g.base_traffic(core)

    def delta(self, group, target):
        g, core = self.g, self.core
        members = set(group)
        if not members or len({core[v] for v in members}) != 1:
            raise ValueError('move requires a nonempty single-core region')
        source = core[group[0]]
        if not 0 <= target < len(self.loads): raise ValueError('invalid target core')
        counts, touched = defaultdict(int), set()
        weights = [0, 0]
        for v in group:
            weights[g.pipe[v]] += g.duration[v]
            touched.update(g.touched[v])
            for t in g.inputs[v]: counts[t] += 1
        work = [row.copy() for row in self.loads]
        for p in (0, 1): work[source][p] -= weights[p]; work[target][p] += weights[p]
        dq = db = dc = 0
        for ti in touched:
            tensor, before = g.tensors[ti], self.counts[ti]
            after = before.copy()
            amount = counts[ti]
            if amount:
                after[source] -= amount
                if not after[source]: del after[source]
                after[target] = after.get(target, 0) + amount
            if tensor.producer is None:
                copies = len(after) - len(before)
            else:
                old_remote = len(set(before) - {core[tensor.producer]})
                owner = target if tensor.producer in members else core[tensor.producer]
                remote_delta = len(set(after) - {owner}) - old_remote
                dc += remote_delta; copies = 2 * remote_delta
            dq += copies * tensor.q; db += copies * tensor.size
        return dict(work=dq, bytes=db, cross_pairs=dc, maximum_load=max(map(max, work), default=0))


def exposure(g, trace):
    ends = {g.dense[op]: end for (_, op), end in zip(trace.op_keys, trace.ends) if op in g.dense}
    if set(ends) != set(range(len(g.ids))): raise ValueError('feedback requires every computation end time')
    return {v: ends[v] + g.b[v] - g.duration[v] for v in ends}


def feedback_proposals(g, parent, trace, *, limit=24, check=lambda: None):
    order = computation_order(g, parent)
    core, rows = parent.core_of_op, parent.compute_orders_by_core
    importance = exposure(g, trace)
    critical = sorted(importance, key=lambda v: (-importance[v], -g.duration[v], g.ids[v]))
    critical = critical[:max(32, math.ceil(len(critical) / 10))]
    if not critical: return
    positions = list(range(min(8, len(critical)))) + [i * (len(critical)-1) // 7 for i in range(8)]
    seeds = list(dict.fromkeys(critical[i] for i in positions))
    index = TrafficIndex(g, core, len(rows))
    position = {v: i for row in rows for i, v in enumerate(row)}
    ranked, seen = defaultdict(list), set()
    for v in seeds:
        check()
        row, at = rows[core[v]], position[v]
        for size in (1, 4, 16, 64):
            left = max(0, at-size//2); right = min(len(row), left+size); left = max(0, right-size)
            group = tuple(row[left:right])
            if group in seen: continue
            seen.add(group)
            for target in range(len(rows)):
                if target == core[v]: continue
                delta = index.delta(group, target)
                key = (delta['maximum_load'] + index.work + delta['work'] +
                       .2 * g.settings.delay * (index.cuts + delta['cross_pairs']),
                       delta['work'], delta['cross_pairs'], g.ids[group[0]], target)
                ranked[len(group)].append((key, group, target, v, delta))
    # Reserve actual-size quotas BEFORE filling globally, never truncate away a
    # reserved larger move after sorting all sizes together.
    ranked = {size: sorted(values, key=lambda x: x[0]) for size, values in sorted(ranked.items())}
    selected, remaining = [], []
    quota = max(1, limit // max(1, len(ranked)))
    for values in ranked.values():
        selected.extend(values[:quota]); remaining.extend(values[quota:])
    selected.extend(sorted(remaining, key=lambda x: x[0])[:max(0, limit-len(selected))])
    for _, group, target, seed, delta in sorted(selected, key=lambda x: x[0])[:limit]:
        check()
        new = list(core)
        for v in group: new[v] = target
        yield atomic_proposal(g, parent, new, order, 'migration',
            dict(seed_op_index=seed, seed_original_id=g.ids[seed], target_core=target,
                 size=len(group), delta=delta, trace_source=trace.score.evaluator_version), group)


def block_proposals(g, parent, trace, preparation, *, limit=24, check=lambda: None):
    importance = exposure(g, trace)
    blocks = [b for row in parent.realized_blocks_by_core for b in row]
    chosen = sorted(blocks, key=lambda b: (-max(importance[v] for v in b.ops), b.core, b.position))[:8]
    seen, actions = {parent.plan_digest}, []
    for block in chosen:
        row = parent.realized_blocks_by_core[block.core]
        for offset in (-1, 1, -4, 4):
            at = block.position + offset
            if not 0 <= at < len(row): continue
            for kind in ('insert', 'swap') if abs(offset) == 1 else ('insert',):
                order = list(range(len(row)))
                if kind == 'insert': order.insert(at, order.pop(block.position))
                else: order[at], order[block.position] = order[block.position], order[at]
                groups = [[b.sgids for b in r] for r in parent.realized_blocks_by_core]
                groups[block.core] = [row[i].sgids for i in order]
                plan = parent.plan
                plan['core_schedules'][block.core] = [sg for group in groups[block.core] for sg in group]
                key = plan_digest(plan)
                if key in seen: continue
                seen.add(key)
                actions.append((plan, groups, block, kind, at))
    for plan, groups, block, kind, at in actions[:limit]:
        check()
        # Keep the declared grouping. Structural/Step1 rejection is handled by
        # the caller, not mistaken for a mutation of the parent.
        candidate = EncodedCandidate(json.dumps(plan), None, 'blocks', None, parent.state_signature)
        yield Proposal(candidate, 'blocks', parent.state_signature, block.ops,
            json.dumps(dict(kind=kind, core=block.core, from_position=block.position,
                            to_position=at, blocks_by_core=groups)))


def capacity_proposals(g, parent, trace, preparation, records, *, check=lambda: None):
    from q2.candidates import capacity_repair_candidates
    if not trace.spill_bytes:
        records.append(dict(status='SKIPPED_NO_SPILL', parent_signature=parent.state_signature))
        return
    options = dict(block_operations=32, window_subgraphs=8, max_windows=8)
    candidate = candidate_from_state(parent)
    windows = []
    for proposed, _, record in capacity_repair_candidates(g, candidate, None, options, check, windows, preparation):
        state = replace(proposed.state, origin=CandidateOrigin('capacity', 'window', None, 'capacity_min_disruption'))
        yield Proposal(candidate_from_state(state), 'capacity', parent.state_signature,
                       parameters_json=json.dumps(record))
    records.extend(windows)
