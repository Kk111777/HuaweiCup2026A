"""Capacity-aware Q3 priority reconstruction, with fixed original-op placement."""
from collections import OrderedDict

from .official import compile_plan
from .scene_b_prefix import build_scene_b_prefix
from .memory_lifetimes import profile
from .capacity_window import CapacityWindow, minimum_disruption, apply_window
from .neighborhoods import Candidate
from .regions import encode_order, operation_cores, projected_order
from .state import PlanState
from .traffic_model import cache_costs, penalties, proposal_cost
from schedule_step2 import step2_spill_insertion


def spill_bytes(records):
    return sum(r['size']*(1+int(r['spill_out_copies_data'])) for r in records)


class MemoryService:
    """Small plan-exact LRU; no state keyed just by partition or core labels."""
    def __init__(self, graph, settings, limit=3):
        self.graph, self.settings, self.limit = graph, settings, limit
        self.cache = OrderedDict()
        self.diagnostics = []

    def prefix(self, graph, plan):
        return build_scene_b_prefix(graph.raw, plan, self.settings['bandwidth'], self.settings['capacity'])

    def get(self, plan, check=lambda: None):
        digest = PlanState(self.graph,plan).digest
        if digest in self.cache:
            self.cache.move_to_end(digest)
            return self.cache[digest]
        check()
        bundle = self.prefix(self.graph,plan)
        for core,c in bundle['cores'].items():
            check()
            c['memory'] = profile(c['graph'],c['seq'],self.settings['capacity'])
            c['spills'] = (step2_spill_insertion(c['graph'],c['seq'],self.settings['capacity'])['spill_records']
                           if c['seq'] else [])
            c['spill_bytes'] = spill_bytes(c['spills'])
        check()
        self.cache[digest] = bundle
        while len(self.cache)>self.limit:
            self.cache.popitem(last=False)
        return bundle

    def record(self, plan, bundle):
        return dict(digest=PlanState(self.graph,plan).digest,
            partition_added_copy_bytes=bundle['base_copy_bytes']-bundle['original_copy_bytes'],
            spill_added_copy_bytes=sum(c['spill_bytes'] for c in bundle['cores'].values()),
            cores={str(k):dict(**c['memory'].record(),spill_bytes=c['spill_bytes'],
                               spill_count=len(c['spills'])) for k,c in bundle['cores'].items()})


def minimum_pressure(window, check=lambda: None):
    """Fallback when no no-spill ordering exists in the fixed window.

Minimize summed per-block overflow exposure then peak and disruption. The
official Step2 still inserts any required spills; this is not a no-spill claim.
"""
    w=window
    costs={0:(0,0,0)}
    paths={0:()}
    for mask in range(1<<w.m):
        check()
        if mask not in costs:
            continue
        for j in range(w.m):
            if mask & (1<<j) or w.pred[j] & ~mask:
                continue
            p,count=w.transition(mask,j,check)
            overflow=sum(max(0,p[k]-w.caps[k])*w.caps[1-k] for k in (0,1))*count
            peak=max(p[k]*w.caps[1-k] for k in (0,1))
            old=costs[mask]
            cost=(old[0]+overflow,max(old[1],peak),old[2]+(((1<<j)-1)&~mask).bit_count())
            nxt=mask|(1<<j)
            path=paths[mask]+(j,)
            if nxt not in costs or (cost,path)<(costs[nxt],paths[nxt]):
                costs[nxt],paths[nxt]=cost,path
    full=(1<<w.m)-1
    if full not in paths:
        return None
    peaks,seq=w.scan(paths[full])
    return dict(status='OPTIMAL_IN_FIXED_WINDOW',scope='minimum_overflow_exposure_not_no_spill',
        block_order=paths[full],subgraph_order=[sg for j in paths[full] for sg in w.blocks[j]],
        predicted_seq=seq,peaks=peaks,pressure=costs[full])


def portfolio_orders(graph, plan, other_plans):
    state=PlanState(graph,plan)
    core=operation_cores(state)
    seen={state.digest}
    for name,other in other_plans:
        order=projected_order(graph,PlanState(graph,other))
        candidate=encode_order(graph,core,order,len(state.queues))
        digest=PlanState(graph,candidate).digest
        if digest in seen:
            continue
        seen.add(digest)
        yield Candidate(candidate,'memory_portfolio_order',
            f'Keep every original-op core; use legal priority projection from {name}; COPY/spills reevaluated.')


def capacity_candidates(graph, plan, result, settings, service, check=lambda: None):
    state=PlanState(graph,plan)
    bundle=service.get(plan,check)
    measured=result['data_movement_bytes']
    original_spill=sum(c['spill_bytes'] for c in bundle['cores'].values())
    partition=bundle['base_copy_bytes']-bundle['original_copy_bytes']
    if (original_spill != measured['spill_added_copy_bytes'] or
            partition != measured['partition_added_copy_bytes']):
        raise ValueError('Q3 memory prefix does not match official measured COPY decomposition')
    service.diagnostics.append(service.record(plan,bundle))
    costs=cache_costs(result)
    weights=penalties(result,costs)
    fifo=costs['bytes'].get('fifo',0)
    seen={state.digest}
    combined={k:list(q) for k,q in enumerate(state.queues)}
    improved_cores=set()
    for core,c in sorted(bundle['cores'].items(),key=lambda kv:-kv[1]['spill_bytes']):
        if not c['spill_bytes'] or len(c['subgraph_order'])<2:
            continue
        queue=c['subgraph_order']
        centers=[]
        for i in (c['memory'].first_overflow,c['memory'].peak_index):
            if i is not None:
                sid=c['op_subgraph'][c['seq'][i]]
                pos=queue.index(sid)
                if pos not in centers: centers.append(pos)
        proposed=[]
        attempted=set()
        for width in (16,64,256,1024):
            span=min(width,len(queue))
            for center in centers[:2]:
                check()
                left=max(0,min(center-span//2,len(queue)-span))
                token=(left,span)
                if token in attempted: continue
                attempted.add(token)
                m=min(8,span)
                blocks=[queue[left+i*span//m:left+(i+1)*span//m] for i in range(m)]
                try:
                    window=CapacityWindow(bundle,core,blocks,settings['capacity'])
                    answer=minimum_disruption(window,check)
                    exact=answer['status']=='OPTIMAL_IN_FIXED_WINDOW'
                    if not exact: answer=minimum_pressure(window,check)
                    if answer is None or list(answer['block_order'])==list(range(m)):
                        continue
                    records=step2_spill_insertion(c['graph'],answer['predicted_seq'],settings['capacity'])['spill_records']
                    new_spill=spill_bytes(records)
                    if new_spill>=c['spill_bytes']:
                        continue
                    candidate,fresh=apply_window(graph,plan,bundle,window,answer,service.prefix)
                    candidate_state=PlanState(graph,candidate)
                    if operation_cores(candidate_state)!=operation_cores(state):
                        raise AssertionError('capacity repair changed placement')
                    total=original_spill-c['spill_bytes']+new_spill
                    # Parent FIFO density is a proposal hint; actual events are
                    # regenerated officially. It is never reported as a hit gain.
                    estimated_fifo=fifo*total/max(1,original_spill)
                    cost=proposal_cost(partition,total,estimated_fifo,weights,settings['bandwidth'])
                    proposed.append((cost,candidate,new_spill,exact,left,span))
                except ValueError as exc:
                    service.diagnostics.append(dict(digest=state.digest,core=core,left=left,span=span,
                                                    status='window_unavailable',reason=str(exc)))
            if proposed:
                # Return useful windows promptly, before a larger scan consumes
                # the complete budget on a single source plan.
                break
        for _,candidate,new_spill,exact,left,span in sorted(proposed,key=lambda x:x[0])[:2]:
            digest=PlanState(graph,candidate).digest
            if digest in seen: continue
            seen.add(digest)
            if core not in improved_cores:
                combined[core]=candidate['core_schedules'][core]
                improved_cores.add(core)
            yield Candidate(candidate,'capacity_window',
                f'Fixed core {core}, {span} buckets at {left}, <=8 macro blocks; '
                f'predicted core spill {c["spill_bytes"]}->{new_spill}; '
                f'no-spill feasible window={exact}; separate penalties={weights}.')
    if len(improved_cores)>1:
        combined_plan=state.with_queues([combined[k] for k in range(len(state.queues))])
        digest=PlanState(graph,combined_plan).digest
        if digest not in seen:
            yield Candidate(combined_plan,'capacity_joint_windows',
                'Jointly apply independent per-core capacity repairs; full official execution validates composition.')
