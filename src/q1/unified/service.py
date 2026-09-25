"""One serial official evaluator, objective cache, and checkpoint boundary per solve."""
from collections import OrderedDict, defaultdict
from time import perf_counter, process_time
from .. import official
from ..plan import plan_digest, validate_plan


class SearchServices:
    def __init__(self, model, settings, *, checkpoint=None, progress=None, save=None, verified=None):
        self.model, self.settings = model, settings
        self.settings_dict = dict(capacity=settings.capacity, bandwidth=settings.bandwidth,
            task_same_core_wait_cycles=settings.same_core_wait,
            task_cross_core_wait_cycles=settings.cross_core_wait)
        self.on_checkpoint, self.progress, self.on_save = checkpoint, progress, save
        self.on_verified = verified
        self.history, self.cache, self.profiles = {}, OrderedDict(), OrderedDict()
        self.stats = defaultdict(float)

    def can_continue(self):
        return True

    def evaluate(self, plan, timeout):
        self.emit(dict(event='phase',phase='official_evaluation'))
        start, cpu = perf_counter(), process_time()
        validate_plan(self.model, plan)
        key = plan_digest(plan)
        hit = key in self.cache
        try:
            result = self.cache[key] if hit else official.evaluate(self.model.raw, plan, self.settings)
            self.cache[key] = result
            self.cache.move_to_end(key)
            while len(self.cache) > 4:
                self.cache.popitem(last=False)
            record = dict(status='ok', cache_hit=hit)
            if self.on_verified:
                self.on_verified(plan,result)
        except official.SCHEDULING_ERRORS as error:
            result = None
            record = dict(status='failed', error=str(error), timed_out=False)
        elapsed = perf_counter()-start
        record.update(seconds=elapsed, cpu_seconds=process_time()-cpu,
                      predicted_cap_seconds=timeout, cap_is_cooperative=True)
        self.stats['official_calls'] += not hit
        self.stats['official_seconds'] += elapsed
        return result, record

    def preanalyze(self, plan):
        # Complete partition identity is required: boundary copies change on split/merge.
        key = tuple(sorted((int(v), t) for v, t in plan['node_to_subgraph'].items()))
        start = perf_counter()
        if key not in self.profiles:
            self.profiles[key] = official.preanalyze(self.model.raw, plan, self.settings)
            while len(self.profiles) > 4:
                self.profiles.popitem(last=False)
        self.stats['preanalysis_seconds'] += perf_counter()-start
        return self.profiles[key]

    def checkpoint(self, plan, result):
        if self.on_checkpoint:
            self.on_checkpoint(plan, result)

    def save(self, history):
        self.history = history
        if self.on_save:
            self.on_save(history)

    def emit(self, event):
        if self.progress:
            self.progress(event)
