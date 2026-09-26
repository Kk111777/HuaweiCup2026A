"""Q2 plateau continuation; only exact feedback is accepted."""
from collections import deque
import copy

POLICIES = ("adaptive", "fixed", "grid")

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
                                  or status not in ('OFFICIAL', 'CACHED_OFFICIAL', 'EQUIVALENT_OFFICIAL')):
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


def __getattr__(name):
    if name == "BetaSearch":
        import warnings
        warnings.warn("Use q2.legacy.beta.LegacyBetaSearch", DeprecationWarning, stacklevel=2)
        from .legacy.beta import LegacyBetaSearch
        return LegacyBetaSearch
    raise AttributeError(name)
