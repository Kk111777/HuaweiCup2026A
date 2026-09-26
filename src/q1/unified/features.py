"""Input-only structural features and online wall-time estimates (not score labels)."""
from collections import Counter
import math
from ..improvement.approximation import original_copy_bytes


def recognize(graph, settings):
    """Computed from the input alone. No case identifiers or archived scores."""
    widths = Counter(graph.level.values())
    work = graph.workload(graph.ops)
    pressure = graph.reference_pressure(frozenset(graph.ops))
    size = len(graph.ops)
    edges = sum(map(len, graph.pred.values()))
    tensor_bytes = sum(t['size'] for t in graph.tensors.values())
    # Count declared COPY traffic, not every tensor once: tensor inventory is
    # neither scheduled traffic nor peak memory. Partition copies are separate.
    copy_bytes = original_copy_bytes(graph)
    chain_sizes = [len(c) for c in graph.chains]
    chain_work = [sum(graph.cost[v] for v in c) for c in graph.chains]
    bandwidth = max(1, settings.get('bandwidth', 1))
    compute = sum(work.values())
    ratio = max((pressure.get(p, 0) / cap for p, cap in settings['capacity'].items()), default=0)
    result = dict(operations=size, original_operations=len(graph.all_ops), tensors=len(graph.tensors),
        dependency_edges=edges, components=len(graph.components), largest_component=max(map(len, graph.components), default=0),
        maximum_level_width=max(widths.values(), default=0), critical_cycles=graph.critical,
        compute_cycles=sum(work.values()), pipe_cycles=dict(work),
        max_fanout=max(map(len, graph.succ.values()), default=0),
        tensor_bytes=tensor_bytes, reference_pressure_bytes=pressure, reference_pressure_ratio=ratio,
        levels=max(graph.level.values(), default=-1)+1,
        chains=len(chain_sizes), maximum_chain_operations=max(chain_sizes, default=0),
        maximum_chain_compute_cycles=max(chain_work, default=0),
        compute_parallelism=compute/max(1,graph.critical),
        dominant_pipe_fraction=max(work.values(), default=0)/max(1,compute),
        original_copy_bytes=copy_bytes, original_copy_cycles_proxy=copy_bytes/bandwidth,
        original_copy_to_compute_ratio=copy_bytes/bandwidth/max(1,compute),
        near_critical_fraction=sum(s <= graph.critical*.05 for s in graph.slack.values())/max(1,size),
        squared_component_operations=sum(len(c)**2 for c in graph.components),
        edge_density=edges/max(1,size),
        note='Input-only reference order pressure; not actual Spill and not a feasibility test.')
    result['traits'] = [name for name, flag in (
        ('wide', result['maximum_level_width'] >= 16), ('disconnected', len(graph.components)>1),
        ('high_fanout', result['max_fanout']>=8), ('memory_pressure', ratio>1),
        ('large', size>=2000)) if flag]
    graph.structural_features = result
    return result


def control_profile(features, cores, seconds):
    """Structure selects search granularity and advisory generation cost priors.

    All values depend on the current input and budget; no case IDs, archived
    scores, single-core runtime or fitted benchmark lookup table is involved.
    """
    n = features['operations']
    deep = features.get('levels', 0) >= 32
    memory_risk = features['reference_pressure_ratio'] > 1
    limited_parallelism = features.get('compute_parallelism', float('inf')) < cores * 1.5
    copy_heavy = features.get('original_copy_to_compute_ratio', 0) > .2
    # These rates only decide whether to start work. They are deliberately
    # advisory; observed phase time replaces them within the same case.
    density = max(1., features['edge_density'])
    scan_work = n*n*density
    generation = dict(construction=.1+scan_work/8_000_000+n/15_000,
                      initial_generation=.1+scan_work/20_000_000+n/10_000,
                      local_generation=.05+scan_work/30_000_000+n/15_000)
    return dict(memory_risk=memory_risk, limited_parallelism=limited_parallelism,
                copy_heavy=copy_heavy,
                generation_seconds_prior=generation,
                calibration_interval_seconds=min(45., seconds*(.1 if memory_risk else .2)),
                # Avoid enumerating a large Cartesian grid before any checkpoint.
                stage_widths=([8,32] if deep else [4,16]), stage_batches=[1,2],
                max_region_candidates=8 if limited_parallelism else 12,
                max_structural_candidates=6 if copy_heavy else 8,
                max_proposals_per_round=24 if n >= 2000 else 48,
                max_cuts_per_task=4 if memory_risk else 3,
                memory_merges=32 if n >= 2000 else 64)


def plan_complexity(graph, plan):
    """Approximate evaluator work: Task preparation + per-event active-op scans.

    Counts structural work, never simulated hardware cycles. Split Tasks reduce
    the quadratic scan term, but duplicate boundary copies increase the linear term.
    """
    counts = Counter(plan['node_to_subgraph'].values())
    owner = {int(v): t for v,t in plan['node_to_subgraph'].items()}
    boundary = Counter()
    for tid in graph.touched(graph.ops):
        producers = {owner[v] for v in graph.producers[tid] if v in owner}
        consumers = {owner[v] for v in graph.consumers[tid] if v in owner}
        for t in consumers-producers: boundary[t] += 1
        final = any(graph.all_ops[v]['op']=='COPY_OUT' for v in graph.consumers[tid])
        for t in producers:
            if final or not consumers or consumers-{t}: boundary[t] += 1
    expanded = [n+boundary[t] for t,n in counts.items()]
    events = sum(expanded)
    # O(n log n) preparation remains even when every Task is small.
    prepare = events*math.log2(max(2,events))
    scans = sum(n*n for n in expanded)
    return dict(tasks=len(counts), events=events, largest_task=max(expanded,default=0),
                squared_task_operations=scans, units=max(1,prepare+scans))


class OfficialCostModel:
    """Online structural cost scaling. Estimates are advisory, never a hard timeout."""
    def __init__(self, graph, baseline_plan=None, baseline_seconds=None):
        self.graph = graph
        self.samples = []
        self.prior = None
        if baseline_plan is not None and baseline_seconds and baseline_seconds>0:
            self.prior = (plan_complexity(graph,baseline_plan)['units'],baseline_seconds)

    def predict(self, complexity):
        units = complexity['units']
        donors = self.samples[-12:] or ([self.prior] if self.prior else [])
        if not donors:
            # A non-zero CPU-work prior makes the first multi-core admission
            # meaningful. Hardware cycles and graph tensor bytes are unrelated
            # to evaluator wall-time and intentionally absent from this formula.
            return max(.05, complexity['units']/2_000_000 +
                       complexity.get('events',0)/20_000 + complexity.get('tasks',0)/2_000)
        nearest = sorted(donors,key=lambda x:abs(math.log(units/x[0])))[:3]
        # Baseline includes a different preparation path: deliberately conservative
        # bootstrap estimate. Real multicore calls replace this prior immediately.
        factor = 2. if not self.samples else 1.35
        values = [seconds*units/work for work,seconds in nearest]
        return max(.001, factor*max(values))

    def observe(self, complexity, record):
        if record['status']=='ok' and not record.get('cache_hit'):
            self.samples.append((complexity['units'],record['seconds']))

    def metadata(self):
        return dict(source='observed_multicore_structure' if self.samples else
                    'singlecore_time_structure_prior' if self.prior else 'input_structure_cold_start',
                    samples=len(self.samples), note='Heuristic wall seconds, separate from cycle prediction; not a bound.')
