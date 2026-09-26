"""One candidate at a time, with independent exact-feedback beta streams."""
from collections import deque
import json

from q2.affinity import component_candidates
from q2.beta import PlateauBeta
from q2.construct import construct_candidate
from q2.encoding import encode_candidate
from .contracts import Proposal


class Construction:
    def __init__(self, graph, cores, *, maximum=4, policy='adaptive'):
        self.g, self.cores = graph, cores
        modes = ['global'] + (['component_grouped'] if len(graph.components) > 1 else [])
        self.beta = PlateauBeta([f'linked/{mode}/{encoding}' for encoding in ('legacy', 'commit', 'tail')
                                  for mode in modes], maximum, policy)
        self.components = deque(component_candidates(graph, cores))
        self.drafts = {}
        self.constructed = 0

    def next(self, *, check=lambda: None):
        check()
        if self.components:
            candidate = self.components.popleft()
            return Proposal(candidate, 'baseline', parameters_json=json.dumps(dict(
                source_family='component', encoding=candidate.encoding)))
        item = self.beta.next()
        if item is None: return None
        stream, beta = item
        _, mode, encoding = stream.split('/')
        key = mode, beta
        if key not in self.drafts:
            self.drafts[key] = construct_candidate(self.g, self.cores, mode, beta, check=check)
            self.constructed += 1
        candidate = encode_candidate(self.g, self.cores, self.drafts[key], encoding)
        return Proposal(candidate, 'baseline', parameters_json=json.dumps(dict(mode=mode, encoding=encoding, beta=beta)),
                        beta_stream=stream, beta=beta)

    def observe(self, proposal, score=None, *, status='OFFICIAL'):
        if proposal.beta_stream is None: return None
        if score is not None and not score.exact:
            raise ValueError('approximate beta feedback is forbidden')
        value = (score.makespan, score.added_copy_bytes) if score is not None else None
        result = self.beta.observe(proposal.beta_stream, proposal.beta, value, status)
        # Retain at most 2 modes x 4 beta immutable drafts, so encodings arriving
        # after independent feedback never repeat a construction.
        return result

    @property
    def exhausted(self):
        return not self.components and not self.beta.queue and not self.beta.pending

    def record(self):
        return dict(self.beta.state_dict(), constructed=self.constructed,
                    component_candidates_remaining=len(self.components))
