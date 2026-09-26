"""Q1 的按模式独立 β 早停规则；反馈必须来自完整 Scene B 官方评价。"""
from collections import deque


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
