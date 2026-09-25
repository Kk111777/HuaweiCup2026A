"""Input-only structural features and online wall-time estimates (not score labels)."""
from collections import Counter
import math


def recognize(graph, settings):
    """Computed before the baseline. No case identifiers or archived scores."""
    widths = Counter(graph.level.values())
    work = graph.workload(graph.ops)
    pressure = graph.reference_pressure(frozenset(graph.ops))
    size = len(graph.ops)
    edges = sum(map(len, graph.pred.values()))
    tensor_bytes = sum(t['size'] for t in graph.tensors.values())
    ratio = max((pressure.get(p, 0) / cap for p, cap in settings['capacity'].items()), default=0)
    result = dict(operations=size, original_operations=len(graph.all_ops), tensors=len(graph.tensors),
        dependency_edges=edges, components=len(graph.components), largest_component=max(map(len, graph.components), default=0),
        maximum_level_width=max(widths.values(), default=0), critical_cycles=graph.critical,
        compute_cycles=sum(work.values()), pipe_cycles=dict(work),
        max_fanout=max(map(len, graph.succ.values()), default=0),
        tensor_bytes=tensor_bytes, reference_pressure_bytes=pressure, reference_pressure_ratio=ratio,
        edge_density=edges/max(1,size),
        note='Input-only reference order pressure; not actual Spill and not a feasibility test.')
    result['traits'] = [name for name, flag in (
        ('wide', result['maximum_level_width'] >= 16), ('disconnected', len(graph.components)>1),
        ('high_fanout', result['max_fanout']>=8), ('memory_pressure', ratio>1),
        ('large', size>=2000)) if flag]
    graph.structural_features = result
    return result


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
        if not donors:return 0.
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
                    'singlecore_time_structure_prior' if self.prior else 'unmeasured',
                    samples=len(self.samples), note='Heuristic wall seconds, separate from cycle prediction; not a bound.')
