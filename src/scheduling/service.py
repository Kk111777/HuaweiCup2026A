"""Serial execution, bounded caches and callbacks for one problem definition."""
from collections import OrderedDict, defaultdict
from time import perf_counter, process_time

from .registry import resolve_problem


class SearchServices:
    def __init__(self, model, settings, *, problem, checkpoint=None, progress=None,
                 save=None, verified=None, rejected=None):
        self.problem = resolve_problem(problem)
        self.model, self.settings = model, settings
        self.settings_dict = self.problem.execution.settings_dict(settings)
        self.on_checkpoint, self.progress, self.on_save = checkpoint, progress, save
        self.on_verified, self.on_rejected = verified, rejected
        self.history, self.cache, self.profiles = {}, OrderedDict(), OrderedDict()
        self.stats = defaultdict(float)
        self.approximation = None
        self.cost_model = None
        self.generation_costs = defaultdict(list)

    def can_continue(self):
        return True

    def _key(self, key):
        return self.problem.id, self.problem.version, key

    def evaluate(self, plan, timeout):
        self.emit(dict(event='phase', phase='official_evaluation'))
        start, cpu = perf_counter(), process_time()
        self.problem.semantics.validate(self.model, plan)
        key = self._key(self.problem.semantics.key(plan))
        hit = key in self.cache
        try:
            result = self.cache[key] if hit else self.problem.execution.evaluate(
                self.model.raw, plan, self.settings)
            self.cache[key] = result
            self.cache.move_to_end(key)
            while len(self.cache) > 4:
                self.cache.popitem(last=False)
            record = dict(status='ok', cache_hit=hit)
            if self.on_verified:
                self.on_verified(plan, result)
        except self.problem.execution.errors as error:
            result = None
            record = dict(status='failed', error=str(error), timed_out=False)
        elapsed = perf_counter()-start
        record.update(seconds=elapsed, cpu_seconds=process_time()-cpu,
                      predicted_cap_seconds=timeout, cap_is_cooperative=True)
        self.stats['official_calls'] += not hit
        self.stats['official_seconds'] += elapsed
        return result, record

    def preanalyze(self, plan):
        key = self._key(self.problem.execution.preanalysis_key(plan))
        start = perf_counter()
        if key not in self.profiles:
            self.profiles[key] = self.problem.execution.preanalyze(self.model.raw, plan, self.settings)
            while len(self.profiles) > 4:
                self.profiles.popitem(last=False)
        self.stats['preanalysis_seconds'] += perf_counter()-start
        return self.profiles[key]

    def checkpoint(self, plan, result):
        if self.on_checkpoint:
            self.on_checkpoint(plan, result)

    def reject(self, plan):
        self.cache.pop(self._key(self.problem.semantics.key(plan)), None)
        if self.on_rejected:
            self.on_rejected(plan)

    def save(self, history):
        self.history = history
        if self.on_save:
            self.on_save(history)

    def emit(self, event):
        if self.progress:
            self.progress(event)
