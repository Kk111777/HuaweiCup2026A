"""Whole-component mapping proposals; no historical solutions are inputs."""
import json
from .contracts import ConstructionDraft, EncodedCandidate
from .encoding import tail_order, encode_atomic

def component_core_map(g, cores) -> tuple[int, ...]:
    """Whole weak computation components; shared source data is NOT a dependency edge."""
    loads = [[0, 0] for _ in range(cores)]
    weighted = []
    for component in g.components:
        w = [sum(g.duration[v] for v in component if g.pipe[v] == p) for p in (0, 1)]
        weighted.append((component, w))
    core = [-1] * len(g.ids)
    for component, work in sorted(weighted, key=lambda cw: (-max(cw[1]), g.ids[cw[0][0]])):
        k = min(range(cores), key=lambda k: (max(loads[k][p]+work[p] for p in (0, 1)),
                                              sum(loads[k])+sum(work), k))
        for v in component: core[v] = k
        for p in (0, 1): loads[k][p] += work[p]
    return tuple(core)


def component_block_plan(g, cores, core) -> dict:
    mapping, queues = {}, [[] for _ in range(cores)]
    for j, component in enumerate(g.components):
        if len({core[v] for v in component}) != 1:
            raise ValueError('component plan requires whole-component affinity')
        queues[core[component[0]]].append(j)
        for v in component: mapping[str(g.ids[v])] = j
    return {'node_to_subgraph': mapping, 'core_schedules': queues}



def component_candidates(g, cores):
    core = component_core_map(g, cores)
    plan = component_block_plan(g, cores, core)
    order = tail_order(g)
    draft = ConstructionDraft(core, order, tuple(map(tuple, g.components)), json.dumps(plan),
                              'component', 'whole_component', None, '{}')
    yield EncodedCandidate(json.dumps(plan), draft, 'block', None)
    yield EncodedCandidate(json.dumps(encode_atomic(g, cores, core, order)), draft, 'tail_atomic', order)
