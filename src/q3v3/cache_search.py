"""Trace-driven, coordinated L2 scheduling for a complete inherited Q1 plan.

The trace is used only with the exact evaluated plan. Timing/slack are hints:
every complete permutation must be compiled and simulated again officially.
No instructions, artificial reads, waits, or cache-control fields are added.
"""
from collections import Counter, defaultdict

from .neighborhoods import Candidate, _l2_candidates, _move, _positions, _timeline
from .state import PlanState, make_plan


def cache_opportunities(state, result, settings):
    timeline = _timeline(result)
    capacity = settings['cache_capacity_bytes']
    inflight, evicted = defaultdict(list), {}
    hints = []
    for event in result.get('cache_events', []):
        tid, kind = event.get('tensor_id'), event.get('event')
        if kind == 'insert':
            for victim in event.get('evicted_tensor_ids', []):
                evicted[victim] = event
            # A new insertion ends the preceding eviction episode.
            evicted.pop(tid, None)
            continue
        if kind != 'miss' or not 0 < event.get('size_bytes', 0) <= capacity:
            continue
        c, t = event['core_id'], event['time']
        op = timeline.get((c, event['op_id']), {})
        sid = op.get('subgraph_id')
        if sid not in state.blocks or state.core_by_block[sid] != c:
            continue
        active = [x for x in inflight[tid] if x['end'] > t and x['core'] != c]
        if active:
            leader = min(active, key=lambda x: (x['end'], x['core'], x['sid']))
            hints.append(dict(kind='inflight', tensor=tid, size=event['size_bytes'],
                core=c, sid=sid, start=t, end=op['end'], leader=leader))
        if tid in evicted:
            eviction = evicted[tid]
            ec = eviction['core_id']
            es = timeline.get((ec, eviction['op_id']), {}).get('subgraph_id')
            if es in state.blocks and state.core_by_block[es] == ec:
                hints.append(dict(kind='fifo', tensor=tid, size=event['size_bytes'],
                    core=c, sid=sid, start=t, end=op['end'],
                    evictor=dict(core=ec, sid=es, time=eviction['time'])))
        inflight[tid].append(dict(core=c, sid=sid, start=t, end=op['end']))
    # Large avoided DDR reads first; use tail room only as a tie-break hint.
    # This is not a proof of slack in the resource-constrained event graph.
    hints.sort(key=lambda h: (-h['size'], h['end'], h['core'], h['sid']))
    return hints, timeline


def _shift(state, core, sid, direction, read_work, gap=0):
    lo, hi = _positions(state, core, sid)
    old = state.position[sid]
    if direction == 'early':
        return _move(state, core, sid, lo) if lo < old else None
    # A compute-only bucket cannot postpone MTE2. Select an actual read window
    # that approaches the observed in-flight completion gap. Moving reads
    # changes DDR contention, so a shorter observed window can still help.
    accumulated = 0
    last = None
    for j in range(old + 1, hi + 1):
        accumulated += read_work[core, state.queues[core][j]]
        if accumulated > 0:
            last = j
        if accumulated > 0 and accumulated >= gap:
            return _move(state, core, sid, j)
    return _move(state, core, sid, last) if last is not None else None


def _coordinated(graph, state, hint, read_work):
    current = state
    changes = 0
    if hint['kind'] == 'inflight':
        leader = hint['leader']
        actions = [(leader['core'], leader['sid'], 'early', 0),
                   (hint['core'], hint['sid'], 'late', max(1, leader['end'] - hint['start']))]
    else:
        evictor = hint['evictor']
        actions = [(hint['core'], hint['sid'], 'early', 0),
                   (evictor['core'], evictor['sid'], 'late', 1)]
    for c, s, direction, gap in actions:
        plan = _shift(current, c, s, direction, read_work, gap)
        if plan is not None:
            current = PlanState(graph, plan)
            changes += 1
    return current, changes


def _expose_trace_order(graph, state, timeline, selected):
    blocks, queues = [], [[] for _ in state.queues]
    for c, queue in enumerate(state.queues):
        for sid in queue:
            if sid not in selected:
                groups = [state.blocks[sid]]
            else:
                def priority(v):
                    op = timeline.get((c, v))
                    return (op['start'], op['end'], graph.rank[v]) if op else (float('inf'), 0, graph.rank[v])
                groups = [[v] for v in sorted(state.blocks[sid], key=priority)]
            for group in groups:
                queues[c].append(len(blocks))
                blocks.append(group)
    # A projection is a candidate, not an assertion of schedule equivalence.
    return PlanState(graph, make_plan(blocks, queues)).plan


def diagnose_cache(graph, plan, result, settings):
    hints, _ = cache_opportunities(PlanState(graph, plan), result, settings)
    reads, sizes = Counter(), {}
    for event in result.get('cache_events', []):
        if event['event'] in ('hit', 'miss') and 0 < event.get('size_bytes', 0) <= settings['cache_capacity_bytes']:
            reads[event['tensor_id']] += 1
            sizes[event['tensor_id']] = event['size_bytes']
    return dict(observed_cacheable_reads=sum(reads.values()),
        repeated_logical_tensors=sum(n > 1 for n in reads.values()),
        ideal_reuse_bytes=sum((n-1)*sizes[t] for t,n in reads.items()),
        inflight_miss_count=sum(h['kind']=='inflight' for h in hints),
        eviction_then_miss_count=sum(h['kind']=='fifo' for h in hints),
        observed_opportunities=hints[:24],
        caveat='Ideal reuse is an optimistic read-byte bound for this execution; not a realizable timing gain.')


def cache_candidates(graph, plan, result, settings):
    state = PlanState(graph, plan)
    hints, timeline = cache_opportunities(state, result, settings)
    if not hints:
        return
    read_work = Counter()
    for (c, _), op in timeline.items():
        if op.get('op') == 'COPY_IN' and op.get('subgraph_id') in state.blocks:
            read_work[c, op['subgraph_id']] += op['duration']
    seen = {state.digest}

    def fresh(candidate):
        digest = PlanState(graph, candidate.plan).digest
        if digest in seen:
            return False
        seen.add(digest)
        return True

    # Cover several independent read windows in one candidate. Each direction
    # obeys current dependencies; old times only prioritize the proposal.
    combined, count = state, 0
    for hint in hints[:12]:
        moved, changes = _coordinated(graph, combined, hint, read_work)
        if changes:
            combined, count = moved, count + changes
    if count:
        item = Candidate(combined.plan, 'l2_joint_windows',
                         f'coordinate {count} dependency-safe priority changes across observed read windows')
        if fresh(item):
            yield item
    hotspots = Counter()
    for hint in hints:
        hotspots[hint['sid']] += hint['size']
        other = hint.get('leader', hint.get('evictor'))
        hotspots[other['sid']] += hint['size']
    hot = [s for s, _ in hotspots.most_common() if len(state.blocks[s]) > 1]
    for selected in (set(hot[:4]), set(hot)):
        if not selected:
            continue
        try:
            exposed = _expose_trace_order(graph, state, timeline, selected)
        except ValueError:
            continue
        item = Candidate(exposed, 'l2_expose_priorities',
                         f'expose observed operation order inside {len(selected)} cache-active Q1 buckets; reevaluate before using new COPY events')
        if fresh(item):
            yield item
    for hint in hints[:12]:
        moved, changes = _coordinated(graph, state, hint, read_work)
        if changes:
            item = Candidate(moved.plan, 'l2_joint_' + hint['kind'],
                             f'jointly adjust source/reuse/evictor priorities for tensor {hint["tensor"]}')
            if fresh(item):
                yield item
    # Keep simpler moves available when a combined window disrupts a useful
    # order. All original generated COPY ids stay tied to this exact state.
    for item in _l2_candidates(graph, state, result, settings):
        if fresh(item):
            yield item
