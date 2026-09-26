"""Independent toy scheduling backend: no Q1 implementation imports.

Jobs run round-robin on lanes. The plan vocabulary deliberately differs from
Q1, so accidental calls into Q1 validation, scheduling or evaluation fail.
All factories are module-level to exercise Windows spawn serialization.
"""
from collections import Counter
import json
from pathlib import Path
import sys

from scheduling.contracts import (ExecutionModel, PlanSemantics, PredictionModel,
                                  ProblemDefinition, ProposalSet)

CALLS = Counter()


def _check_no_q1(raw):
    if raw.get('require_no_q1_imports'):
        imported = [name for name in sys.modules if name == 'q1' or name.startswith('q1.')]
        if imported:
            raise AssertionError(f'Q1 imported during independent backend execution: {imported}')


class ToyModel:
    def __init__(self, raw):
        _check_no_q1(raw)
        self.raw = raw
        self.ops = {job['id']: job['work'] for job in raw['jobs']}
        self.signature_calls = []


def read_settings(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def settings_dict(settings):
    return dict(settings)


def create_index(model, settings):
    _check_no_q1(model.raw)
    return dict(job_count=len(model.ops))


def validate(model, plan, cores=None):
    _check_no_q1(model.raw)
    if set(plan) != {'order', 'lanes', 'label'}:
        raise ValueError('not a toy plan')
    if sorted(plan['order']) != sorted(model.ops) or len(plan['order']) != len(model.ops):
        raise ValueError('toy jobs must be covered exactly once')
    if cores is not None and plan['lanes'] != cores:
        raise ValueError('wrong lane count')
    # This is intentionally not an iterable of partition blocks.
    return 12345


def key(plan):
    return json.dumps(plan, sort_keys=True)


def objective(result):
    return (result['makespan'],)


def state_signature(model, plan, cores=None):
    validate(model, plan, cores)
    signature = ('toy-order', tuple(plan['order']), plan['lanes'])
    model.signature_calls.append(signature)
    return signature


def evaluate(raw, plan, settings):
    _check_no_q1(raw)
    CALLS['evaluate'] += 1
    jobs = {job['id']: job['work'] for job in raw['jobs']}
    lanes = [0] * plan['lanes']
    for index, job in enumerate(plan['order']):
        lanes[index % plan['lanes']] += jobs[job]
    return dict(makespan=max(lanes, default=0) * settings['multiplier'],
                evaluation_source='official', oracle='independent_toy')


def preanalysis_key(plan):
    CALLS['preanalysis_key'] += 1
    # A job profile is independent of ordering/labels in this toy model.
    return tuple(sorted(plan['order']))


def preanalyze(raw, plan, settings):
    _check_no_q1(raw)
    CALLS['preanalyze'] += 1
    return {job['id']: job['work'] * settings['multiplier'] for job in raw['jobs']}


def empty_result(cores):
    return dict(makespan=0, evaluation_source='official', oracle='independent_toy', lanes=cores)


def create_estimator(model, settings, options):
    _check_no_q1(model.raw)
    return None


def create_policy(seconds, options):
    raise AssertionError('this backend is official-only')


class ToyCostModel:
    def __init__(self, model, baseline_plan=None, baseline_seconds=None):
        _check_no_q1(model.raw)
        self.samples = 0

    def predict(self, complexity):
        return .001

    def observe(self, complexity, record):
        self.samples += 1

    def metadata(self):
        return dict(source='toy_cost', samples=self.samples)


def recognize(model, settings):
    _check_no_q1(model.raw)
    model.structural_features = dict(operations=len(model.ops))
    return model.structural_features


def complexity(model, plan):
    _check_no_q1(model.raw)
    return dict(units=len(plan['order']) + 1)


def proxy(model, plan, settings):
    return evaluate(model.raw, plan, settings)['makespan']


def seed(model, cores, settings):
    return dict(order=sorted(model.ops), lanes=cores, label='seed'), dict(method='toy_seed')


class ToyConstructor:
    def __init__(self, model, cores, settings, service):
        _check_no_q1(model.raw)
        self.records = []

    def next(self, seconds=None):
        return None

    def observe(self, name, result):
        pass


def initial(model, cores, settings, params, audit=None):
    _check_no_q1(model.raw)
    order = sorted(model.ops)
    if len(order) != 4:
        return []
    first = dict(order=[order[0], order[1], order[3], order[2]], lanes=cores, label='balanced')
    second = dict(order=list(reversed(order)), lanes=cores, label='reverse')
    return [('toy_balance', first, proxy(model, first, settings)),
            ('toy_reverse', second, proxy(model, second, settings))]


def local(model, plan, result, settings, params):
    _check_no_q1(model.raw)
    return [], 0


def rebuild(model, validated, result, cores, scale):
    _check_no_q1(model.raw)
    return []


def schedule(model, blocks, cores, settings):
    raise AssertionError('toy rebuild produces no partitions to schedule')


def retain(states, incoming, limit):
    CALLS['retain'] += 1
    for state in [*states, incoming]:
        expected = ('toy-order', tuple(state['plan']['order']), state['plan']['lanes'])
        if state['signature'] != expected:
            raise AssertionError('common search did not use the injected state signature')
    unique = {s['signature']: s for s in states}
    existing = unique.get(incoming['signature'])
    if existing is None or incoming['objective'] < existing['objective']:
        unique[incoming['signature']] = incoming
    return sorted(unique.values(), key=lambda s: (s['objective'], s['digest']))[:limit]


def control_profile(features, cores, seconds):
    return dict(generation_seconds_prior=dict(construction=.001, initial_generation=.001,
                                              local_generation=.001),
                calibration_interval_seconds=7., max_initial_evaluations_per_core=1)


def default_parameters():
    return dict(evaluation=dict(mode='official'), search_states=3,
                max_initial_evaluations_per_core=2, max_rounds=0,
                no_improvement_rounds=3, official_per_round=1)


def definition():
    return ProblemDefinition(id='toy_lanes', version='test-1',
        semantics=PlanSemantics(validate, key, objective, state_signature),
        execution=ExecutionModel(read_settings, settings_dict, ToyModel, create_index,
            evaluate, preanalyze, preanalysis_key, (ValueError,), empty_result),
        prediction=PredictionModel(create_estimator, create_policy, ToyCostModel, recognize,
            complexity, proxy, 'toy_round_robin', 'Exact tiny lane workload for interface tests.'),
        proposals=ProposalSet(seed, ToyConstructor, initial, local, rebuild, schedule, retain,
            control_profile, ('max_initial_evaluations_per_core',)),
        default_parameters=default_parameters, supported_cores=(1, 2, 7))


def raw_input(strict=False):
    return dict(jobs=[dict(id=i, work=w) for i, w in enumerate((8, 7, 6, 5))],
                require_no_q1_imports=strict)


def spawn_probe(problem, sender):
    try:
        sender.send(dict(id=problem.id, version=problem.version,
                         result=problem.execution.empty_result(problem.supported_cores[0]),
                         has_defaults=isinstance(problem.default_parameters(), dict)))
    finally:
        sender.close()
