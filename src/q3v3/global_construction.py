"""Independent whole-graph constructions with N-dependent granularity."""
from .growth import grow
from .construction import place_blocks
from .neighborhoods import Candidate
from .regions import RegionIndex, coarsen_regions, encode_order, pack_regions, topological_order
from .state import PlanState, make_plan


class RegionLibrary:
    """One graph/N-local cache shared by construction and joint neighborhoods."""
    def __init__(self, graph, cores, settings):
        self.graph, self.cores, self.settings = graph, cores, settings
        self.cache = {}

    def get(self, beta):
        if beta not in self.cache:
            self.cache[beta] = coarsen_regions(self.graph, self.cores, self.settings, beta)
        return self.cache[beta]


def region_order(index):
    graph = index.graph
    tail = [max((graph.backward[v] for v in b), default=0) for b in index.groups]
    return topological_order(graph, {v: (-tail[index.owner[v]], index.owner[v],
                                        -graph.backward[v], v) for v in graph.ops})


def global_candidates(graph, cores, settings, library=None, check=lambda: None):
    """No inherited plan is an input; every candidate is a full Q3 proposal.

    Bounded alternatives are retained even after a plateau: beta is not known
    to have a monotone relation with official makespan. Source Q2's strict
    first-plateau early stop is therefore intentionally not copied.
    """
    library = library or RegionLibrary(graph, cores, settings)
    seen = set()
    components = RegionIndex(graph, graph.components, settings)
    assignment = pack_regions(components, cores)
    core = components.cores(assignment)
    queues = [[] for _ in range(cores)]
    for i in range(len(components.groups)):
        queues[assignment[i]].append(i)
    # A single whole component exposes no parallel placement. The validated
    # initial seed remains the fallback; first cover the chain constructor.
    if len(components.groups) > 1:
        plan = make_plan(components.groups, queues)
        seen.add(PlanState(graph, plan).digest)
        yield Candidate(plan, 'global_component', 'Whole computation components packed by persistent Pipe load.')
    # These are graph-native maximal unbranched chains, not a refinement of
    # an imported partition. Producer-release placement is shared with the
    # existing Q3 proposer; all three variants get independent official tests.
    chain_groups = sorted(graph.chains, key=lambda b: min(graph.rank[v] for v in b))
    chain_plan = make_plan(chain_groups, [list(range(len(chain_groups)))] + [[] for _ in range(cores-1)])
    for weight in (.5, 1., 2.):
        check()
        plan = place_blocks(graph, chain_plan, settings, communication_weight=weight)
        digest = PlanState(graph, plan).digest
        if digest not in seen:
            seen.add(digest)
            yield Candidate(plan, 'global_chain', f'Native complete chains with producer-release placement, affinity={weight}.')
    if len(components.groups) > 1:
        plan = encode_order(graph, core, region_order(components), cores)
        if PlanState(graph, plan).digest not in seen:
            seen.add(PlanState(graph, plan).digest)
            yield Candidate(plan, 'global_component', 'Same whole-component mapping with atomic compute priority.')

    def growth_candidate(beta):
        check()
        try:
            core, order, owner, info = grow(graph, cores, settings, beta, check)
        except ValueError:
            return None
        plan = encode_order(graph, core, order, cores)
        digest = PlanState(graph, plan).digest
        if digest not in seen:
            seen.add(digest)
            return Candidate(plan, 'global_growth', f'Ready-node/core joint growth: {info}')
        return None

    # Cover growth before enumerating every coarse-region encoding.
    proposal = growth_candidate(1)
    if proposal:
        yield proposal
    # Each family is independently constructed. Neither graph names nor
    # measured single-core/cached Q1/Q2 scores are arguments to these APIs.
    for beta, affinity in ((4, 0.0), (2, .25), (1, .25), (4, 1.0), (2, 1.0)):
        check()
        index = library.get(beta)
        assigned = pack_regions(index, cores, affinity)
        core, order = index.cores(assigned), region_order(index)
        for encoding in ('atomic', 'runs'):
            plan = encode_order(graph, core, order, cores, encoding=encoding, region_of=index.owner)
            digest = PlanState(graph, plan).digest
            if digest not in seen:
                seen.add(digest)
                yield Candidate(plan, 'global_region',
                    f'Heavy-edge regions beta={beta}, affinity={affinity}, regions={len(index.groups)}, encoding={encoding}.')
    proposal = growth_candidate(4)
    if proposal:
        yield proposal
