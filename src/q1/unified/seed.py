"""Graph-only bootstrap with legacy verified single-core seed compatibility."""
from collections import Counter
from copy import deepcopy
from math import ceil
from ..improvement.schedule import schedule


def structural_plan(model, cores, settings):
    """Build a legal graph-only plan; the caller supplies its shared evaluator.

    Only complete weak components are packed together. Larger components are
    split at contiguous topological boundaries, preserving the quotient DAG.
    A bounded Task size avoids both a single expensive Task and per-op Tasks.
    """
    if type(cores) is not int or cores not in (2, 3, 4, 5):
        raise ValueError('cores must be 2, 3, 4 or 5')
    if not isinstance(settings, dict):
        settings = dict(capacity=settings.capacity, bandwidth=settings.bandwidth,
                        task_same_core_wait_cycles=settings.same_core_wait,
                        task_cross_core_wait_cycles=settings.cross_core_wait)
    if not model.ops:
        return ({'node_to_subgraph': {}, 'core_schedules': [[] for _ in range(cores)]},
                dict(method='structural_bootstrap', tasks=0, largest_task=0,
                     components=0, proxy_cycles=0, score_source='unscored'))
    features = getattr(model, 'structural_features', {})
    pressure = features.get('reference_pressure_bytes')
    if pressure is None:
        pressure = model.reference_pressure(frozenset(model.ops))
    pressure_ratio = max((pressure.get(p, 0) / cap
                          for p, cap in settings['capacity'].items()), default=0)
    # Pressure is only a risk signal, never a feasibility certificate.
    maximum = 256 if pressure_ratio > 1 else 512
    components = sorted(model.components, key=lambda b: min(model.rank[v] for v in b))
    complete, chunks = [], []
    for component in components:
        if len(component) <= maximum and len(components) > 1:
            complete.append(component)
            continue
        ordered = sorted(component, key=model.rank.__getitem__)
        total = sum(model.cost[v] for v in component)
        critical = max(model.forward[v] for v in component)
        parallelism = max(1, total / max(1, critical))
        parts = max(ceil(len(component) / maximum),
                    min(cores * 2, ceil(parallelism)))
        target_work = total / parts
        target_size = max(1, ceil(len(component) / parts))
        block, work = [], 0
        for v in ordered:
            block.append(v)
            work += model.cost[v]
            next_nodes = model.succ[v]
            in_chain = (len(next_nodes) == 1 and
                        len(model.pred[next(iter(next_nodes))]) == 1)
            if (len(block) >= maximum or
                    (not in_chain and len(block) >= target_size and work >= target_work)):
                chunks.append(frozenset(block))
                block, work = [], 0
        if block:
            chunks.append(frozenset(block))
    if complete:
        count = min(len(complete), max(cores, ceil(sum(map(len, complete)) / maximum)))
        bins, loads = [set() for _ in range(count)], [Counter() for _ in range(count)]
        weighted = [(c, model.workload(c)) for c in complete]
        for component, work in sorted(weighted, key=lambda item: (
                -max(item[1].values(), default=0), -sum(item[1].values()),
                min(model.rank[v] for v in item[0]))):
            eligible = [i for i, b in enumerate(bins) if len(b) + len(component) <= maximum]
            if not eligible:
                eligible = [len(bins)]
                bins.append(set())
                loads.append(Counter())
            chosen = min(eligible, key=lambda i: (
                max((loads[i][p] + work[p] for p in loads[i].keys() | work.keys()), default=0),
                sum(loads[i].values()) + sum(work.values()), len(bins[i]), i))
            bins[chosen].update(component)
            loads[chosen].update(work)
        chunks.extend(frozenset(b) for b in bins if b)
    chunks.sort(key=lambda b: min(model.rank[v] for v in b))
    plan, proxy = schedule(model, chunks, cores, settings)
    return plan, dict(method='structural_bootstrap', tasks=len(chunks),
        largest_task=max(map(len, chunks)), maximum_task_operations=maximum,
        components=len(components), packed_components=len(complete),
        reference_pressure_ratio=pressure_ratio, proxy_cycles=proxy,
        score_source='unscored',
        note='Graph-only partition and shared EFT; proxy is not an official makespan.')

def extend_empty_cores(plan, result, cores):
    plan,result=deepcopy(plan),deepcopy(result)
    old_cores=len(plan['core_schedules'])
    if cores<old_cores:
        raise ValueError('cannot shrink a verified prior-core solution')
    plan['core_schedules'].extend([] for _ in range(cores-old_cores))
    result['num_cores']=cores
    result['memory_peak_by_core']={str(k):v for k,v in result['memory_peak_by_core'].items()}
    for c in range(old_cores,cores):
        result['memory_peak_by_core'][str(c)]={'L1':0,'UB':0}
        result['per_core_timeline'].append({'core_id':c,'tasks':[],'subgraphs':[],'ops':[]})
    result.pop('execution_mode',None)
    return plan,result
