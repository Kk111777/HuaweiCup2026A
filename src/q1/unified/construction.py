"""Collaborator task growth as a lazy proposal provider in the shared search."""
from collections import deque
from time import perf_counter
from ..graph import GraphIndex
from ..partition import partition, BETAS
from ..schedule import assign_cores
from .. import official
from ..improvement.schedule import cheap_plan_proxy
from ..plan import validate_plan


class _ConstructionWindowExpired(Exception):
    pass


class Construction:
    def __init__(self, model, cores, settings, service):
        self.model, self.cores, self.settings, self.service = model, cores, settings, service
        self.index = getattr(service, 'graph_index', None)
        if self.index is None:
            self.index = GraphIndex(model, settings)
        elif self.index.model is not model or self.index.settings != settings:
            raise ValueError('shared GraphIndex must belong to this graph and settings')
        self.scopes = dict(self.index.scopes(cores))
        # Disconnected regions have no cross-component data dependencies. Give
        # the component-aware constructor the first opportunity; the global
        # alternative remains a proposal in the same controller, not a second solve.
        modes = sorted(self.scopes, key=lambda mode: mode != 'components')
        self.queue = deque((mode, BETAS[0]) for mode in modes)
        self.waiting, self.best, self.records = {}, {}, []
        self.approximation = None

    def next(self, seconds=None):
        """Optionally bound generation at existing progress/profile checkpoints.

        This is a cooperative window, not a hard deadline. The case supervisor
        still owns absolute protection, and the already saved seed survives.
        No argument preserves the legacy construction behavior.
        """
        if seconds is not None and seconds <= 0:
            return None
        started = perf_counter()
        self._deadline = None if seconds is None else started + seconds
        self._active_name = None
        try:
            return self._next()
        except _ConstructionWindowExpired:
            self.records.append(dict(action=self._active_name, status='window_expired',
                                     seconds=perf_counter()-started,
                                     note='Cooperative construction window; retained prior checkpoint.'))
            return None
        finally:
            self._deadline = None

    def _check_window(self):
        if self._deadline is not None and perf_counter() >= self._deadline:
            raise _ConstructionWindowExpired()

    def _next(self):
        if not self.queue:
            return None
        self._check_window()
        mode, beta = self.queue.popleft()
        name = f'growth_{mode}_beta{beta}'
        self._active_name = name
        self.service.emit(dict(event='phase',phase='growth_partition',action=name))
        def progress(event):
            self._check_window()
            self.service.emit(dict(event,phase='growth_partition',action=name))
        tasks, growth = partition(self.index, self.scopes[mode], self.cores, beta,
            progress=progress)
        self._check_window()
        repairs = []
        approximate = self.approximation is not None and (
            getattr(self.service, 'shared_case', False) or len(self.model.ops) >= 2000)
        while True:
            temporary = official.make_plan(tasks, self.cores)
            try:
                self.service.emit(dict(event='phase',phase='growth_preanalysis',action=name))
                if approximate:
                    blocks = validate_plan(self.model, temporary)
                    durations = {}
                    for i, block in enumerate(blocks):
                        self._check_window()
                        durations[i] = self.approximation._profile(block)['duration']
                else:
                    durations = self.service.preanalyze(temporary)
                break
            except official.LOCAL_ERRORS as error:
                repairs.append(str(error))
                if all(len(t) <= 1 for t in tasks):
                    self.records.append(dict(action=name, status='infeasible', repairs=repairs))
                    return None
                tasks = [part for t in tasks for part in
                         ([t] if len(t) <= 1 else [t[:len(t)//2], t[len(t)//2:]])]
        self.service.emit(dict(event='phase',phase='growth_allocation',action=name))
        self._check_window()
        plan, allocation = assign_cores(self.model, temporary, durations, self.settings)
        self.waiting[name] = (mode, beta)
        self.records.append(dict(action=name, status='generated', repairs=repairs,
                                 timing_source='approximate_local_pipe' if approximate else 'official_preanalysis',
                                 tasks=len(tasks), growth=growth, allocation=allocation))
        return name, plan, cheap_plan_proxy(self.model, plan, self.service.settings_dict)

    def observe(self, name, result):
        """Advance beta only on measured improvement, never archived scores."""
        spec = self.waiting.pop(name, None)
        if spec is None:
            return
        mode, beta = spec
        previous = self.best.get(mode)
        improved = result is not None and (previous is None or result['makespan'] < previous)
        if improved:
            self.best[mode] = result['makespan']
        if beta < BETAS[-1] and improved:
            self.queue.append((mode, beta+1))
