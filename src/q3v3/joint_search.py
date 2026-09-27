"""Atomic region regrouping, multi-move core refinement and order rebuilding.

The local pass uses a best-prefix search: individual intermediate moves need
not improve its proxy. Only the complete resulting plan is officially tested.
This permits coordinated rebalancing across a flat multi-core maximum.
"""
from collections import Counter

from .global_construction import RegionLibrary, region_order
from .neighborhoods import Candidate
from .regions import encode_order, locality_order, operation_cores, projected_order
from .state import PlanState


def refine_assignment(index, assignment, cores, affinity=.25, check=lambda: None, *, movable=None):
    """Bounded Fiduccia--Mattheyses-style best-prefix moves on logical regions.

    The heuristic is max Pipe load + weighted cut cost / N. A cut's 500-cycle
    term is an affinity signal, not a claim that all delays add serially.
    At most 64 high-work/high-cut regions and two passes are considered.
    """
    current = dict(assignment)
    eligible = set(current) if movable is None else set(movable) & current.keys()
    loads = [Counter() for _ in range(cores)]
    for i, k in current.items():
        loads[k].update(index.work[i])

    def cut_value(mapping):
        return sum(value for i, links in enumerate(index.links) for j, value in links.items()
                   if i < j and mapping[i] != mapping[j])

    def peak(rows):
        return max((max(row.values(), default=0) for row in rows), default=0)

    cut = cut_value(current)
    best_score = peak(loads) + affinity * cut / cores
    best = dict(current)
    for _ in range(2):
        check()
        by_work = sorted(eligible, key=lambda i: (-max(index.work[i].values(), default=0), i))[:32]
        by_cut = sorted(eligible, key=lambda i: (-sum(value for j, value in index.links[i].items()
                         if current[i] != current[j]), i))[:32]
        unlocked = set(by_work + by_cut)
        improved = False
        while unlocked:
            check()
            choice = None
            for i in sorted(unlocked):
                a = current[i]
                after_a = loads[a].copy()
                after_a.subtract(index.work[i])
                for b in range(cores):
                    if a == b:
                        continue
                    after_b = loads[b] + index.work[i]
                    delta = sum(value * (int(current[j] != b) - int(current[j] != a))
                                for j, value in index.links[i].items())
                    new_peak = max(max(after_a.values(), default=0), max(after_b.values(), default=0),
                        max((max(row.values(), default=0) for k, row in enumerate(loads) if k not in (a, b)), default=0))
                    score = new_peak + affinity * (cut + delta) / cores
                    # On a flat maximum, reduce dispersion instead of draining
                    # one arbitrary core while several others remain critical.
                    dispersion = max(after_a.values(), default=0)**2 + max(after_b.values(), default=0)**2
                    dispersion += sum(max(row.values(), default=0)**2 for k,row in enumerate(loads) if k not in (a,b))
                    key = (score, dispersion, cut + delta, i, b)
                    if choice is None or key < choice[0]:
                        choice = (key, i, a, b, after_a, after_b, delta)
            if choice is None:
                break
            key, i, a, b, after_a, after_b, delta = choice
            loads[a], loads[b] = after_a, after_b
            current[i] = b
            cut += delta
            unlocked.remove(i)
            if key[0] < best_score - 1e-8:
                best_score, best, improved = key[0], dict(current), True
        current = dict(best)
        loads = [Counter() for _ in range(cores)]
        for i, k in current.items():
            loads[k].update(index.work[i])
        cut = cut_value(current)
        if not improved:
            break
    return best


def joint_candidates(graph, plan, result, settings, library=None, check=lambda: None):
    state = PlanState(graph, plan)
    cores = len(state.queues)
    library = library or RegionLibrary(graph, cores, settings)
    inherited = operation_cores(state)
    seen = {state.digest}

    def candidate(core, order, reason, region_of=None, encoding='atomic'):
        proposed = encode_order(graph, core, order, cores, encoding=encoding, region_of=region_of)
        digest = PlanState(graph, proposed).digest
        if digest not in seen:
            seen.add(digest)
            return Candidate(proposed, 'joint_region', reason)

    # Preserve inherited placement while exposing priority, then repair
    # static local lifetimes. Q2's exact fixed-window capacity DP is *not*
    # claimed here: COPY/spill events and L2 remain official-only feedback.
    for order, reason in ((projected_order(graph, state), 'Preserve core placement and approximate inherited queue priorities.'),
                          (locality_order(graph, inherited, memory=True), 'Rebuild ready order using local last uses.')):
        check()
        proposal = candidate(inherited, order, reason)
        if proposal:
            yield proposal
    for beta in (4, 2, 1):
        check()
        index = library.get(beta)
        assignment = index.majority(inherited)
        # This can split multiple inherited buckets and reunite their nodes;
        # no single-bucket improvement is required before testing the package.
        for affinity in (.15, 1.0):
            refined = refine_assignment(index, assignment, cores, affinity, check)
            core = index.cores(refined)
            changed = sum(core[v] != inherited[v] for v in graph.ops)
            order = region_order(index)
            proposal = candidate(core, order,
                f'Joint regroup/reassign/order beta={beta}, affinity={affinity}, '
                f'regions={len(index.groups)}, moved_operations={changed}.')
            if proposal:
                yield proposal
            if affinity == .15:
                proposal = candidate(core, order,
                    f'Same joint region mapping beta={beta} with contiguous-run submission buckets.',
                    index.owner, 'runs')
                if proposal:
                    yield proposal
