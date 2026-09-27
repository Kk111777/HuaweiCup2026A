"""Bounded Q3 placement/order proposals driven by dependencies and COPY events.

These proposals never prescribe a delay, cache insertion, or Task barrier.
Only the official evaluator can accept the resulting operation/Pipe schedule.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import heapq

from .state import PlanState


@dataclass(frozen=True)
class Candidate:
    plan: dict
    family: str
    reason: str


def _move(state, core, sid, position):
    queues = [list(q) for q in state.queues]
    queues[core].remove(sid)
    queues[core].insert(position, sid)
    try:
        return state.with_queues(queues)
    except ValueError:
        return None


def _positions(state, core, sid):
    """Inclusive insertion interval after removing sid; direct edges suffice."""
    others = [s for s in state.queues[core] if s != sid]
    pos = {s: i for i, s in enumerate(others)}
    lo = max((pos[p] + 1 for p in state.pred[sid] if p in pos), default=0)
    hi = min((pos[t] for t in state.succ[sid] if t in pos), default=len(others))
    return lo, hi


def _timeline(result):
    return {(row['core_id'], op['op_id']): op
            for row in (result or {}).get('per_core_timeline', [])
            for op in row.get('ops', [])}


def _load_data(graph, state):
    work = {s: graph.workload(b) for s, b in state.blocks.items()}
    core_work = [Counter() for _ in state.queues]
    for s, loads in work.items():
        core_work[state.core_by_block[s]].update(loads)
    critical = {s: max((graph.backward[v] for v in b), default=0)
                for s, b in state.blocks.items()}
    return work, core_work, critical


def _order_candidates(graph, state, work, critical, result):
    # Bucket priorities affect all pipes, including generated COPY_OUT. There is
    # no Q1 whole-subgraph completion penalty in any score here.
    for mode, priority in (
        ('critical_release', {s: (-critical[s], max(work[s].values(), default=0))
                              for s in state.blocks}),
        ('short_ready', {s: (max(work[s].values(), default=0), -critical[s])
                         for s in state.blocks}),
    ):
        yield Candidate(state.rebuild(priority=priority), 'order',
                        f'{mode}: dependency-ready buckets receive global priority')

    # Pull valuable releases from any queue position, not just its tail.
    ranked = sorted(state.blocks, key=lambda s: (-critical[s], s))
    count = 0
    for s in ranked:
        c = state.core_by_block[s]
        lo, _ = _positions(state, c, s)
        if lo >= state.position[s]:
            continue
        plan = _move(state, c, s, lo)
        if plan:
            yield Candidate(plan, 'order', f'advance dependency-ready block {s} on core {c}')
            count += 1
            if count >= 8:
                break

    # Actual late releases identify a different order neighborhood than graph
    # criticality alone. These are hints, not causal performance predictions.
    late = sorted((result or {}).get('cross_core_transfers', []),
                  key=lambda e: (-(e.get('copy_in_end', 0)), e.get('tensor_id', -1)))
    timeline = _timeline(result)
    count = 0
    for event in late:
        c = event['source_core']
        op = timeline.get((c, event['source_copy_out_id']), {})
        s = op.get('subgraph_id')
        if s not in state.blocks:
            continue
        lo, _ = _positions(state, c, s)
        if lo >= state.position[s]:
            continue
        plan = _move(state, c, s, lo)
        if plan:
            yield Candidate(plan, 'release',
                            f'advance source block {s}; observed COPY_IN completion '
                            f'{event.get("copy_in_end")} for tensor {event.get("tensor_id")}')
            count += 1
            if count >= 6:
                break


def _placement_candidates(graph, state, work, core_work, critical, settings):
    n = len(state.queues)
    if n < 2:
        return
    pressure = [max(w.values(), default=0) for w in core_work]
    targets = sorted(range(n), key=lambda c: (pressure[c], c))
    sources = sorted(range(n), key=lambda c: (-pressure[c], c))
    offered = 0
    for c in sources[:min(3, n)]:
        # All positions are eligible. Large indivisible buckets are left intact;
        # the caller's structural refinement owns partition changes.
        blocks = sorted(state.queues[c], key=lambda s: (-max(work[s].values(), default=0),
                                                        -critical[s], s))
        for s in blocks[:6]:
            for target in [t for t in targets if t != c][:2]:
                assignment = dict(state.core_by_block)
                assignment[s] = target
                yield Candidate(state.rebuild(assignment), 'placement',
                                f'move block {s} from core {c} to {target}; '
                                f'fixed-placement pipe loads {pressure[c]} -> {pressure[target]}')
                offered += 1
                if offered >= 16:
                    break
            if offered >= 16:
                break
        if offered >= 16:
            break

    # A coordinated reassignment can improve a flat maximum where one move
    # cannot. Transfer penalties count core crossings, never Task boundaries.
    bandwidth = float(settings['bandwidth'])
    cross_wait = float(settings['cross_core_copy_delay_cycles'])
    edges = defaultdict(float)
    for tid, consumers in graph.consumers.items():
        producers = graph.producers[tid] & graph.ops.keys()
        for p in {state.owner[v] for v in producers}:
            for t in {state.owner[v] for v in consumers & graph.ops.keys()} - {p}:
                edges[p, t] += graph.tensors[tid]['size'] / bandwidth
    for affinity_weight in (0.0, 0.25, 1.0):
        assigned, loads = {}, [Counter() for _ in range(n)]
        degree = {s: len(p) for s, p in state.pred.items()}
        ready = [(-critical[s], s) for s, d in degree.items() if not d]
        heapq.heapify(ready)
        while ready:
            _, s = heapq.heappop(ready)
            scores = []
            for c in range(n):
                peak = max((loads[c][p] + work[s][p] for p in loads[c].keys() | work[s].keys()),
                           default=0)
                crossing = sum(cross_wait + edges[p, s] for p in state.pred[s]
                               if assigned[p] != c)
                scores.append((peak + affinity_weight * crossing,
                               c != state.core_by_block[s], c))
            c = min(scores)[-1]
            assigned[s] = c
            loads[c].update(work[s])
            for t in sorted(state.succ[s]):
                degree[t] -= 1
                if not degree[t]:
                    heapq.heappush(ready, (-critical[t], t))
        yield Candidate(state.rebuild(assigned, {s: -critical[s] for s in state.blocks}),
                        'rebalance', f'joint pipe-load reassignment with cross-core affinity {affinity_weight}')

    # Co-locate a producer with a group of its consumers atomically. This makes
    # the search sensitive to reused tensors rather than individual DAG edges.
    ranked_tensors = []
    for tid, consumers in graph.consumers.items():
        ps = {state.owner[v] for v in graph.producers[tid] & graph.ops.keys()}
        cs = {state.owner[v] for v in consumers & graph.ops.keys()}
        all_blocks = ps | cs
        touched = {state.core_by_block[s] for s in all_blocks}
        if ps and len(touched) > 1 and 1 < len(all_blocks) <= 12:
            ranked_tensors.append((-(graph.tensors[tid]['size'] * (len(touched) - 1)), tid, ps, cs))
    for _, tid, ps, cs in sorted(ranked_tensors)[:6]:
        for target in sorted({state.core_by_block[s] for s in ps}):
            assignment = dict(state.core_by_block)
            for s in ps | cs:
                assignment[s] = target
            yield Candidate(state.rebuild(assignment), 'affinity',
                            f'co-locate producer/consumer group for tensor {tid} on core {target}')


def _l2_candidates(graph, state, result, settings):
    """Use actual miss completion and FIFO eviction evidence, not a hit proxy."""
    capacity = settings['cache_capacity_bytes']
    timeline = _timeline(result)
    events = (result or {}).get('cache_events', [])
    misses = defaultdict(list)
    collision_hints = []
    eviction_hints = []
    last_eviction = {}
    for event in events:
        tid, kind = event.get('tensor_id'), event.get('event')
        if kind == 'insert':
            for evicted in event.get('evicted_tensor_ids', []):
                last_eviction[evicted] = event
            last_eviction.pop(tid, None)
            continue
        if kind != 'miss' or not 0 < event.get('size_bytes', 0) <= capacity:
            continue
        c, op_id = event['core_id'], event['op_id']
        op = timeline.get((c, op_id), {})
        s = op.get('subgraph_id')
        if s not in state.blocks or state.core_by_block[s] != c:
            continue
        t = event['time']
        inflight = [(end, other_c) for end, other_c in misses[tid]
                    if end > t and other_c != c]
        if inflight:
            leader_end, _ = min(inflight)
            collision_hints.append((-event['size_bytes'], c, s, tid, t, leader_end))
        if tid in last_eviction:
            eviction_hints.append((-event['size_bytes'], c, s, tid, last_eviction[tid]))
        misses[tid].append((op.get('end', t), c))

    # Delaying compute alone does not delay MTE2. Require an actual independent
    # COPY_IN bucket in the shifted interval, so the proposal can change reads.
    read_blocks = defaultdict(set)
    for (c, _), op in timeline.items():
        if op.get('op') == 'COPY_IN' and op.get('subgraph_id') in state.blocks:
            read_blocks[c].add(op['subgraph_id'])
    emitted = 0
    for _, c, s, tid, t, leader_end in sorted(collision_hints):
        _, hi = _positions(state, c, s)
        old = state.position[s]
        candidates = [j for j in (old + 1, old + 2, old + 4, hi)
                      if old < j <= hi and any(b in read_blocks[c] for b in state.queues[c][old + 1:j + 1])]
        for j in dict.fromkeys(candidates):
            plan = _move(state, c, s, j)
            if plan:
                yield Candidate(plan, 'l2_inflight',
                                f'tensor {tid} duplicated miss at {t} before another read completes '
                                f'at {leader_end}; move consumer bucket {s} behind existing reads on core {c}')
                emitted += 1
                if emitted >= 16:
                    break
        if emitted >= 16:
            break

    emitted = 0
    for _, c, s, tid, eviction in sorted(eviction_hints, key=lambda h: h[:4]):
        lo, _ = _positions(state, c, s)
        if lo < state.position[s]:
            plan = _move(state, c, s, lo)
            if plan:
                yield Candidate(plan, 'l2_fifo',
                                f'advance reuse of tensor {tid}; previous copy was evicted at {eviction["time"]}')
                emitted += 1
        ec = eviction.get('core_id')
        evictor = timeline.get((ec, eviction.get('op_id')), {}).get('subgraph_id')
        if evictor in state.blocks and ec == state.core_by_block[evictor]:
            _, hi = _positions(state, ec, evictor)
            if hi > state.position[evictor]:
                plan = _move(state, ec, evictor, min(hi, state.position[evictor] + 2))
                if plan:
                    yield Candidate(plan, 'l2_fifo',
                                    f'move FIFO-evicting input bucket {evictor} later; '
                                    f'tensor {tid} has observed reuse after eviction')
                    emitted += 1
        if emitted >= 12:
            break


def generate_candidates(graph, plan, result, settings, *, max_candidates=48, enable_l2=True):
    """Return deduplicated complete plans, round-robin across available families.

    ``result`` is an official Q3 result for exactly ``plan``. The caller owns
    that association and must re-evaluate every returned proposal officially.
    No measured single-core score or case name is accepted by this interface.
    """
    if max_candidates <= 0:
        return []
    state = PlanState(graph, plan)
    work, core_work, critical = _load_data(graph, state)
    grouped = defaultdict(list)
    # Retain the more specific event evidence when an order proposal is also
    # reachable by a generic move. Disabling L2 does not forbid such generic
    # moves; it removes the cache-based cause and priority only.
    streams = [_l2_candidates(graph, state, result, settings)] if enable_l2 else []
    streams.extend([_order_candidates(graph, state, work, critical, result),
                    _placement_candidates(graph, state, work, core_work, critical, settings)])
    seen = {state.digest}
    for stream in streams:
        for candidate in stream:
            digest = PlanState(graph, candidate.plan).digest
            if digest not in seen:
                seen.add(digest)
                grouped[candidate.family].append(candidate)
    # The L2 families have first access to a small budget; each later pass gives
    # every nonempty family one slot, preventing prolific moves from starving it.
    families = ['l2_inflight', 'l2_fifo', 'order', 'release', 'placement', 'rebalance', 'affinity']
    chosen = []
    while len(chosen) < max_candidates:
        progress = False
        for family in families:
            if grouped[family] and len(chosen) < max_candidates:
                chosen.append(grouped[family].pop(0))
                progress = True
        if not progress:
            break
    return chosen
