"""Cost-ranked cross-core read windows, after private-cache repair proposals."""
from collections import Counter

from .cache_search import cache_opportunities, _coordinated, cache_candidates
from .neighborhoods import Candidate
from .state import PlanState
from .traffic_model import penalties


def window_candidates(graph,plan,result,settings):
    state=PlanState(graph,plan)
    hints,timeline=cache_opportunities(state,result,settings)
    weights=penalties(result)
    read_work=Counter()
    for (core,_),op in timeline.items():
        if op.get('op')=='COPY_IN' and op.get('subgraph_id') in state.blocks:
            read_work[core,op['subgraph_id']]+=op['duration']
    def benefit(h):
        weight=weights['fifo'] if h['kind']=='fifo' else weights['partition']
        saved=h['size']*(1/settings['bandwidth']-1/settings['cache_bandwidth_bytes_per_cycle'])
        gap=max(0,h['leader']['end']-h['start']) if h['kind']=='inflight' else 0
        # A conservative ranking signal, not resource-constrained slack.
        lateness=h['end']/max(1,result['makespan'])
        return weight*saved/(1+lateness*gap/max(1,saved))
    hints=sorted(hints,key=lambda h:(-benefit(h),h['tensor'],h['core'],h['sid']))
    seen={state.digest}
    for count in (3,8,16):
        current=state
        changed=0
        for hint in hints[:count]:
            current,n=_coordinated(graph,current,hint,read_work)
            changed+=n
        if changed and current.digest not in seen:
            seen.add(current.digest)
            yield Candidate(current.plan,'l2_cost_windows',
                f'Coordinate {count} observed read windows; separate partition/FIFO penalties={weights}; '
                'old times only rank; no explicit wait or added operation.')
    for candidate in cache_candidates(graph,plan,result,settings):
        digest=PlanState(graph,candidate.plan).digest
        if digest not in seen:
            seen.add(digest)
            yield candidate
