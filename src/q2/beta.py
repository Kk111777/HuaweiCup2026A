"""Q1 的按模式独立 β 早停规则；反馈必须来自完整 Scene B 官方评价。"""
from collections import deque
import copy


POLICIES = ('adaptive', 'fixed', 'grid')


class BetaSearch:
    def __init__(self, modes, betas, policy):
        if policy not in POLICIES:
            raise ValueError('beta_search must be adaptive, fixed or grid')
        self.betas = [1] if policy == 'fixed' else list(betas)
        self.policy = policy
        self.pending = deque((mode, self.betas[0]) for mode in modes)
        self.states = {mode: dict(mode=mode, evaluated_betas=[], best_beta=None,
                                 best_objective=None, stop_reason=None) for mode in modes}
        self.previous = {}
        self.report = dict(policy=policy, betas=self.betas,
                           continuation={'adaptive': 'strict_makespan_decrease',
                                         'fixed': 'beta_1_only', 'grid': 'all_betas'}[policy],
                           scheduling='round_robin_modes', modes=list(self.states.values()),
                           evaluated_candidates=0, successful_candidates=0, cache_hits=0,
                           official_evaluations=0, fallback_used=False)

    def next_candidate(self):
        return self.pending.popleft() if self.pending else None

    def observe(self, mode, beta, value, *, cached=False):
        """value 为官方 (makespan, added_copy_bytes)，失败为 None。"""
        state = self.states[mode]
        previous = self.previous.get(mode)
        state['evaluated_betas'].append(beta)
        self.report['evaluated_candidates'] += 1
        self.report['cache_hits'] += int(cached)
        if value is not None:
            value = tuple(value)
            self.report['successful_candidates'] += 1
            if state['best_objective'] is None or value < tuple(state['best_objective']):
                state.update(best_beta=beta, best_objective=value)
        improved = value[0] < previous[0] if value is not None and previous is not None else None
        reason = None
        if self.policy == 'fixed':
            reason = 'fixed_beta_complete'
        elif self.policy == 'adaptive' and value is None:
            reason = 'candidate_failed'
        elif self.policy == 'adaptive' and previous is not None and not improved:
            reason = 'no_makespan_improvement'
        elif beta == self.betas[-1]:
            reason = 'max_beta_reached' if self.policy == 'adaptive' else 'grid_exhausted'
        next_beta = None if reason else self.betas[self.betas.index(beta)+1]
        if next_beta is not None:
            self.pending.append((mode, next_beta))
        self.previous[mode] = value
        state['stop_reason'] = reason
        return dict(previous_makespan=previous[0] if previous is not None else None,
                    strictly_improved=improved, next_beta=next_beta, stop_reason=reason)

    def stop(self, reason):
        self.pending.clear()
        for state in self.states.values():
            if state['stop_reason'] is None:
                state['stop_reason'] = reason


class PlateauBeta:
    """Per-stream adjacent-beta feedback. Equality CONTINUES; strict increase stops.

    No historical-best envelope participates in this comparison. Invalid candidates
    break adjacency; subsequent valid candidates re-anchor. Timeouts stop this
    stream as incomplete, never as evidence of a worse numerical objective.
    """
    def __init__(self, streams, maximum=4, policy="adaptive", continuation="plateau"):
        if policy not in POLICIES or continuation not in ("plateau", "strict"):
            raise ValueError("invalid beta policy")
        self.policy, self.continuation = policy, continuation
        if type(maximum) is not int or maximum < 1:
            raise ValueError('maximum beta must be a positive integer')
        self.maximum = 1 if policy == "fixed" else maximum
        self.queue = deque((s, 1) for s in streams)
        self.previous = {}
        self.records = []
        self.stops = {}
        self.pending = {}
        self.best = {}

    def next(self):
        if not self.queue: return None
        stream, beta = self.queue.popleft()
        if stream in self.pending: raise RuntimeError('feedback missing for stream')
        self.pending[stream] = beta
        return stream, beta

    def observe(self, stream, beta, value, status='OFFICIAL'):
        if self.pending.get(stream) != beta:
            raise ValueError('stale or unordered beta feedback')
        if value is not None and (len(value) != 2 or any(type(x) is not int or x < 0 for x in value)
                                  or status not in ('OFFICIAL', 'CACHED_OFFICIAL')):
            raise ValueError('beta feedback requires exact official objective')
        self.pending.pop(stream)
        previous = self.previous.get(stream)
        if value is not None:
            value = tuple(value)
            if stream not in self.best or value < self.best[stream]: self.best[stream] = value
            worsening = previous is not None and (value[0] > previous[0] or
                (self.continuation == "strict" and value[0] == previous[0]))
            worsening = worsening and self.policy == "adaptive"
            reason = ('STRICT_TIME_INCREASE' if self.continuation == 'plateau' else 'NO_TIME_IMPROVEMENT') if worsening else ('MAX_BETA' if beta == self.maximum else None)
            relation = ('first' if previous is None else
                        'worse' if value[0] > previous[0] else 'equal' if value[0] == previous[0] else 'better')
            self.previous[stream] = value
        elif status == 'INVALID':
            self.previous.pop(stream, None)  # beta gap is not an observed T(beta)
            relation, reason = 'invalid', 'MAX_BETA' if beta == self.maximum else None
        else:
            relation, reason = 'unknown', 'INCOMPLETE_EVALUATION'
        record = {'stream': stream, 'beta': beta, 'previous': previous, 'objective': value,
                  'relation': relation, 'status': status, 'stop_reason': reason}
        self.records.append(record)
        if reason: self.stops[stream] = reason
        else: self.queue.append((stream, beta+1))
        return record


    def stop(self, reason):
        for stream, _ in self.queue:
            self.stops.setdefault(stream, reason)
        for stream in self.pending:
            self.stops.setdefault(stream, reason)
        self.queue.clear()
        self.pending.clear()

    def state_dict(self):
        return copy.deepcopy(dict(version='Q2-Beta-2.0', maximum=self.maximum, policy=self.policy,
                    continuation=self.continuation, queue=list(self.queue), previous=dict(self.previous),
                    records=list(self.records), stops=dict(self.stops), pending=dict(self.pending), best=dict(self.best)))

    @classmethod
    def from_state_dict(cls, state):
        if state.get('version') != 'Q2-Beta-2.0':
            raise ValueError('beta state version mismatch')
        obj = cls([], state['maximum'], state['policy'], state['continuation'])
        obj.queue = deque(tuple(x) for x in state['queue'])
        for field in ('previous', 'records', 'stops', 'pending', 'best'):
            setattr(obj, field, copy.deepcopy(state[field]))
        obj.previous = {k: tuple(v) for k, v in obj.previous.items()}
        obj.best = {k: tuple(v) for k, v in obj.best.items()}
        return obj
