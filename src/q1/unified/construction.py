"""Collaborator task growth as a lazy proposal provider in the shared search."""
from collections import deque
from ..graph import GraphIndex
from ..partition import partition, BETAS
from ..schedule import assign_cores
from .. import official
from ..improvement.schedule import cheap_plan_proxy
from ..plan import validate_plan


class Construction:
    def __init__(self, model, cores, settings, service):
        self.model, self.cores, self.settings, self.service = model, cores, settings, service
        self.index = GraphIndex(model, settings)
        self.scopes = dict(self.index.scopes(cores))
        # Disconnected regions have no cross-component data dependencies. Give
        # the component-aware constructor the first opportunity; the global
        # alternative remains a proposal in the same controller, not a second solve.
        modes = sorted(self.scopes, key=lambda mode: mode != 'components')
        self.queue = deque((mode, BETAS[0]) for mode in modes)
        self.waiting, self.best, self.records = {}, {}, []
        self.approximation = None

    def next(self):
        if not self.queue:
            return None
        mode, beta = self.queue.popleft()
        name = f'growth_{mode}_beta{beta}'
        self.service.emit(dict(event='phase',phase='growth_partition',action=name))
        tasks, growth = partition(self.index, self.scopes[mode], self.cores, beta,
            progress=lambda event: self.service.emit(dict(event,phase='growth_partition',action=name)))
        repairs = []
        approximate = self.approximation is not None and len(self.model.ops) >= 2000
        while True:
            temporary = official.make_plan(tasks, self.cores)
            try:
                self.service.emit(dict(event='phase',phase='growth_preanalysis',action=name))
                if approximate:
                    blocks = validate_plan(self.model, temporary)
                    durations = {i:self.approximation._profile(b)['duration'] for i,b in enumerate(blocks)}
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
