"""Separate observed cut, spill and FIFO penalties for candidate generation.

FIFO rereads overlap logical COPY. Never add these fields and call that total
traffic; delivery uses official added COPY and physical DDR independently.
"""
from collections import Counter, defaultdict


def cache_costs(result):
    ends = {(c['core_id'],o['op_id']):o['end'] for c in result['per_core_timeline'] for o in c['ops']}
    seen, evicted = set(), set()
    active = defaultdict(list)
    totals = Counter()
    per_tensor = defaultdict(Counter)
    for e in result.get('cache_events', ()):
        tid = e['tensor_id']
        if e['event'] == 'insert':
            evicted.update(e.get('evicted_tensor_ids', ()))
            evicted.discard(tid)
            continue
        if e['event'] not in ('hit','miss'):
            continue
        live = [end for end in active[tid] if end > e['time']]
        kind = ('hit' if e['event']=='hit' else 'first_read' if tid not in seen
                else 'inflight' if live else 'fifo' if tid in evicted else 'other')
        totals[kind] += e['size_bytes']
        per_tensor[tid][kind] += e['size_bytes']
        if e['event'] == 'miss':
            active[tid] = live + [ends[e['core_id'],e['op_id']]]
        seen.add(tid)
    return dict(bytes=dict(totals), per_tensor={k:dict(v) for k,v in per_tensor.items()})


def penalties(result, costs=None):
    costs = cache_costs(result) if costs is None else costs
    m = result['data_movement_bytes']
    total = max(1,m['added_copy_bytes'])
    miss = max(1,result['cache_stats']['miss_bytes'])
    return dict(partition=1 + m['partition_added_copy_bytes']/total,
                spill=1 + 2*m['spill_added_copy_bytes']/total,
                fifo=1 + 2*costs['bytes'].get('fifo',0)/miss)


def proposal_cost(partition_bytes, spill_bytes, fifo_reread_bytes, weights, bandwidth):
    """A labeled proposal penalty in cycle units, NOT a predicted makespan.

FIFO is an additional urgency signal on overlapping bytes. Final scoring does
not include this signal, avoiding double counting in reported traffic totals.
"""
    return (weights['partition']*partition_bytes + weights['spill']*spill_bytes
            + weights['fifo']*fifo_reread_bytes) / bandwidth
