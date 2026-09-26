"""Two model levels, bounded raw-result caching and same-input calibration."""
from collections import defaultdict, deque
from dataclasses import replace
import math
from statistics import median
from time import perf_counter

from q2.contracts import ScoreRecord
from q2.evaluation import binding
from .preparation import PreparationService
from .contracts import ModelTrace
from .events import network, lower_bound, simulate

MODEL_VERSION = 'Q2-EventModel-1.0'


class Calibration:
    def __init__(self):
        self.samples = defaultdict(lambda: deque(maxlen=8))
        self.seen = set()
        self.version = 0

    @staticmethod
    def bucket(trace):
        return trace.level, trace.pressure > 1 or (trace.spill_bytes or 0) > 0

    def observe(self, trace, official):
        if not official.exact or official.source not in ('official', 'equivalent_official'):
            raise ValueError('calibration requires official evidence')
        if (trace.score.environment_digest, trace.score.execution_digest) != (
                official.environment_digest, official.execution_digest):
            raise ValueError('calibration execution identity differs')
        key = trace.score.environment_digest, trace.score.execution_digest, self.bucket(trace)
        if key in self.seen or trace.raw_cycles <= 0 or official.makespan <= 0:
            return False
        self.seen.add(key)
        self.samples[self.bucket(trace)].append(math.log(official.makespan / trace.raw_cycles))
        self.version += 1
        return True

    def correct(self, raw, level, pressure, spill, bound):
        values = self.samples.get((level, pressure > 1 or (spill or 0) > 0), ())
        rho = len(values) / (len(values) + 4) * median(values) if values else 0.
        return max(bound, math.ceil(raw * math.exp(rho)))

    def record(self):
        return dict(version=self.version, samples={f'{level}/{int(pressure)}': list(values)
                    for (level, pressure), values in self.samples.items()}, shrinkage=4, window=8)


class CostModel:
    """Host seconds only; learned estimates never replace the hard supervisor."""
    def __init__(self, graph=None):
        self.samples = defaultdict(lambda: deque(maxlen=8))
        self.timeouts = defaultdict(float)

    def estimate(self, stage, complexity=1):
        values = self.samples.get(stage, ())
        return max(.01, self.timeouts[stage],
                   2 * median(values) * max(1, complexity) if values else .01)

    def observe(self, stage, seconds, complexity=1, *, cached=False, timed_out=False):
        if cached:
            return
        if timed_out:
            self.timeouts[stage] = max(self.timeouts[stage], seconds * 2)
        elif seconds > 0:
            self.samples[stage].append(seconds / max(1, complexity))

    def record(self):
        return dict(normalized_seconds={k: list(v) for k, v in self.samples.items()},
                    timeout_estimates=dict(self.timeouts))


class ModelEvaluator:
    def __init__(self, graph, preparation=None, calibration=None, costs=None):
        self.g = graph
        self.preparation = preparation or PreparationService(graph)
        self.calibration = calibration or Calibration()
        self.costs = costs or CostModel(graph)
        self.stats = defaultdict(float)

    def _bind(self, state, level, data, elapsed):
        cycles = self.calibration.correct(data['raw_cycles'], level, data['pressure'],
                                          data['spill_bytes'], data['lower_bound'])
        score = ScoreRecord(**binding(state), makespan=cycles, added_copy_bytes=data['added_copy_bytes'],
            source='approximate', exact=False,
            evaluator_version=f'{MODEL_VERSION}/{level}/calibration-{self.calibration.version}',
            calibrated_from=f'same-input-official-v{self.calibration.version}')
        return ModelTrace(level, data['raw_cycles'], score, data['lower_bound'], data['pressure'],
                          data['spill_bytes'], elapsed, data['events'],
                          data['op_keys'], data['starts'], data['ends'])

    def evaluate(self, state, level='refined', *, check=lambda: None):
        if level not in ('coarse', 'refined'):
            raise ValueError('unknown model level')
        started = perf_counter()
        # Current state consistency is mandatory even if numerical work is cached.
        from q2.state import validate_state
        validate_state(self.g, state, preparation=self.preparation)
        key = (state.environment_digest, MODEL_VERSION, level, state.execution_digest)
        cache = self.preparation.cache
        data = cache.get('model', key)
        if data is not None:
            self.stats[level + '_hits'] += 1
            return self._bind(state, level, data, perf_counter() - started)
        check()
        bundle = self.preparation.prefix(state.plan)
        profiles = self.preparation.profiles(bundle)
        pressure = max((p.peaks[j] / self.g.settings.capacity[pos]
                        for p in profiles.values() for j, pos in enumerate(('L1', 'UB'))), default=0.)
        # No invented FIFO on the base necessary path.
        bound = lower_bound(network(bundle, self.g.settings, include_pipes=False))
        prepared = self.preparation.prepare_execution(state, check=check) if level == 'refined' else None
        net = network(bundle, self.g.settings, prepared.tasks if prepared else None)
        result = simulate(net, trace=level == 'refined', check=check)
        if prepared is not None:
            bound = max(bound, lower_bound(net))
        traffic = prepared.traffic if prepared else None
        data = dict(raw_cycles=result['makespan'], lower_bound=bound, pressure=pressure,
                    # Base traffic is not the unknown total added traffic of a coarse candidate.
                    added_copy_bytes=traffic['added_copy_bytes'] if traffic else None,
                    spill_bytes=traffic['spill_added_copy_bytes'] if traffic else None,
                    events=result['events'], op_keys=net.ids if prepared else (),
                    starts=result['starts'], ends=result['ends'])
        cache.put('model', key, data)
        elapsed = perf_counter() - started
        self.stats[level + '_evaluations'] += 1
        self.stats[level + '_seconds'] += elapsed
        self.costs.observe(level, elapsed, max(1, len(net.ids)) ** 2)
        return self._bind(state, level, data, elapsed)

    def rescore(self, state, trace):
        score = replace(trace.score, **binding(state), makespan=self.calibration.correct(
            trace.raw_cycles, trace.level, trace.pressure, trace.spill_bytes, trace.lower_bound),
            evaluator_version=f'{MODEL_VERSION}/{trace.level}/calibration-{self.calibration.version}',
            calibrated_from=f'same-input-official-v{self.calibration.version}')
        return replace(trace, score=score)
