"""Complete the existing baseline families inside the same service and pool.

This is not a second solver: construction, exact caches, current states, budget,
and pool are shared with local search. No historical solution is read.
"""
from dataclasses import asdict
from time import perf_counter

from q2.evaluation import better
from q2.construct import BudgetExpired
from .proposals import capacity_proposals
from .service import scheduling_errors


class BaselineProtection:
    def __init__(self, session):
        self.session = session
        self.phase = 'initial'
        self.best = None
        self.b0 = None
        self.windows = None
        self.complete = False
        self.unknown = False
        self.requests = 0
        self.stop_reason = None

    def measure(self, state, proposal=None):
        s = self.session
        s.check()
        estimate = s.service.model.costs.estimate('official', max(1, len(s.g.ids))**2)
        if not s.budget.can_start(estimate): raise BudgetExpired('baseline_guard_budget')
        timeout = min(s.budget.candidate_cap(), max(0., s.turn_deadline-perf_counter()))
        row, record = s.service.official(state, timeout, check=s.check)
        self.requests += 1
        s.record('baseline_guard_evaluation', phase=self.phase, state_signature=state.state_signature,
                 score=asdict(row.score) if row else None, record=record)
        if row is None:
            self.unknown = True
            s.budget.observe(None, None, dict(timed_out=record.get('status') == 'EVAL_TIMEOUT', timeout_seconds=timeout))
            return
        s.budget.observe(None, {'makespan':row.score.makespan},
                         dict(seconds=record.get('wall_seconds',0), evaluation_source=row.score.source))
        if proposal is not None: s.constructor.observe(proposal, row.score,
            status='EQUIVALENT_OFFICIAL' if row.score.source=='equivalent_official' else 'CACHED_OFFICIAL' if record.get('cached') else 'OFFICIAL')
        if self.best is None or better(row.score, self.best.score): self.best = row
        s.pool.update([row])

    def step(self):
        """One candidate per turn, so all requested core counts share progress."""
        s = self.session
        if self.phase == 'done': return False
        s.check()
        if self.phase == 'initial':
            self.phase = 'construction'
            if s.pool.incumbents.search is not None: self.measure(s.pool.incumbents.search.state)
            return True
        if self.phase == 'construction':
            proposal = s.constructor.next(check=s.check)
            if proposal is not None:
                try:
                    state = s.service.state(proposal, check=s.check)
                    self.measure(state, proposal)
                except (ValueError, *scheduling_errors()) as error:
                    s.record('baseline_guard_invalid', reason=str(error), beta=proposal.beta, stream=proposal.beta_stream)
                    if proposal.beta_stream is not None: s.constructor.observe(proposal, status='INVALID')
                return True
            self.b0 = self.best
            self.phase = 'capacity'
            if self.b0 is not None:
                trace = s.service.model.evaluate(self.b0.state, check=s.check)
                self.windows = capacity_proposals(s.g, self.b0.state, trace, s.service.preparation,
                                                 s.windows, check=s.check)
        if self.phase == 'capacity':
            try:
                proposal = next(self.windows) if self.windows is not None else None
            except StopIteration:
                proposal = None
            if proposal is not None:
                try:
                    state = s.service.state(proposal, self.b0.state, check=s.check)
                    self.measure(state)
                except scheduling_errors() as error:
                    s.record('baseline_guard_invalid_window', reason=str(error))
                return True
            self.phase = 'done'
            self.complete = self.best is not None and not self.unknown and s.constructor.exhausted
            self.stop_reason = 'COMPLETE' if self.complete else 'INCOMPLETE_EVALUATION'
            s.record('baseline_guard_complete', **self.record())
        return False

    def finish(self):
        """Measure the selected estimate if time allows, else retain exact best.

        Selection happens before the output is frozen. Independent replay cannot
        change it. A caller never needs to trust an estimated improvement.
        """
        s = self.session
        best = s.pool.incumbents.search
        exact = s.pool.incumbents.exact
        if best is None or best.score.exact or (exact is not None and not better(best.score, exact.score)): return
        estimate = s.service.model.costs.estimate('official', max(1, len(s.g.ids))**2)
        if not s.budget.can_start(estimate):
            s.record('guard_publish_fallback', reason='insufficient_time'); return
        try:
            timeout = min(s.budget.candidate_cap(), max(0., s.turn_deadline-perf_counter()))
            row, record = s.service.official(best.state, timeout, check=s.check)
            if row is not None: s.pool.update([row])
            s.record('guard_publish_verification', state_signature=best.state.state_signature,
                     score=asdict(row.score) if row else None, record=record)
        except (BudgetExpired, MemoryError, *scheduling_errors()) as error:
            s.record('guard_publish_fallback', reason=type(error).__name__)

    def record(self):
        row = self.best
        return dict(enabled=True, complete=self.complete, phase=self.phase,
                    stop_reason=self.stop_reason, evaluation_requests=self.requests,
                    baseline_makespan=row.score.makespan if row else None,
                    baseline_added_copy_bytes=row.score.added_copy_bytes if row else None,
                    baseline_state_signature=row.state.state_signature if row else None,
                    baseline_b0_makespan=self.b0.score.makespan if self.b0 else None)
