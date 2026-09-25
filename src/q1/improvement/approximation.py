"""Cached task DAGs and a small event simulator for candidate screening.

This models local Pipe order and shared DDR service, but does not claim to
reproduce official spill insertion or memory reuse dependencies.
"""
from collections import Counter
import heapq
import math

from .partition import quotient


def task_profile(graph, block, settings):
    bandwidth=settings['bandwidth']
    touched=sorted(graph.touched(block));reads=[];writes=[]
    for tid in touched:
        producers=graph.producers[tid]&block;consumers=graph.consumers[tid]&block
        eligible=graph.consumers[tid]&graph.ops.keys()
        if consumers and not producers:reads.append(tid)
        if producers and (not eligible or eligible-block or any(
                graph.all_ops[v]['op']=='COPY_OUT' for v in graph.consumers[tid])):writes.append(tid)
    # Boundary IDs need the same relative ordering as the official constructor.
    if not hasattr(graph,'_approx_copy_id_base'):
        graph._approx_copy_id_base=max(max(graph.all_ops,default=0),max(graph.tensors,default=0))+1
    next_id=graph._approx_copy_id_base
    read_tensors=set(reads);write_tensors=set(writes)
    read_ids={};write_ids={}
    for tid in touched:
        if tid in read_tensors:read_ids[tid]=next_id;next_id+=1
        if tid in write_tensors:write_ids[tid]=next_id;next_id+=1
    pred={v:set(graph.pred[v]&block) for v in block}
    duration={v:graph.cost[v] for v in block};pipe={v:graph.ops[v]['pipe'] for v in block}
    for tid,v in read_ids.items():
        pred[v]=set();duration[v]=max(1,math.ceil(graph.tensors[tid]['size']/bandwidth));pipe[v]='PIPE_MTE2'
        for child in graph.consumers[tid]&block:pred[child].add(v)
    for tid,v in write_ids.items():
        pred[v]=set(graph.producers[tid]&block)
        duration[v]=max(1,math.ceil(graph.tensors[tid]['size']/bandwidth));pipe[v]='PIPE_MTE3'
    succ={v:set() for v in pred}
    for v,ps in pred.items():
        for p in ps:succ[p].add(v)
    depth={}
    for v in list(read_ids.values())+sorted(block,key=graph.rank.__getitem__)+list(write_ids.values()):
        depth[v]=1+max((depth[p] for p in pred[v]),default=-1)
    read_set=set(read_ids.values());write_set=set(write_ids.values())
    key=lambda v:(v not in read_set,depth[v],-v)
    stack=sorted((v for v in pred if not succ[v]),key=lambda v:(v not in write_set,depth[v],-v))
    visited=set();order=[]
    while stack:
        v=stack[-1]
        if v in visited:stack.pop();continue
        pending=pred[v]-visited
        if pending:stack.extend(sorted(pending,key=key))
        else:visited.add(v);order.append(v);stack.pop()
    if len(order)!=len(pred):raise ValueError('approximation task is cyclic')
    index={v:i for i,v in enumerate(order)};last_pipe={};ends={}
    predecessors=[]
    for v in order:
        ps=set(pred[v]);previous=last_pipe.get(pipe[v])
        if previous is not None:ps.add(previous)
        last_pipe[pipe[v]]=v
        predecessors.append([index[p] for p in ps])
        ends[v]=duration[v]+max((ends[p] for p in ps),default=0)
    following=[[] for _ in order]
    for i,ps in enumerate(predecessors):
        for parent in ps:following[parent].append(i)
    # Pressure is measured on this local order, including boundary lifetimes.
    events={}
    for tid in touched:
        uses=[index[v] for v in (graph.producers[tid]|graph.consumers[tid])&block]
        if tid in read_ids:uses.append(index[read_ids[tid]])
        if tid in write_ids:uses.append(index[write_ids[tid]])
        if not uses:continue
        tensor=graph.tensors[tid];pos='UB' if tensor['pos']=='DDR' else tensor['pos']
        events.setdefault(2*min(uses),Counter())[pos]+=tensor['size']
        events.setdefault(2*max(uses)+1,Counter())[pos]-=tensor['size']
    live=Counter();pressure=Counter()
    for event in sorted(events):
        live.update(events[event])
        for pos in settings['capacity']:pressure[pos]=max(pressure[pos],live[pos])
    return dict(duration=max(ends.values(),default=1),
        boundary_bytes=sum(graph.tensors[t]['size'] for t in reads+writes),
        pressure=dict(pressure),pressure_excess_bytes=sum(max(0,pressure[p]-cap) for p,cap in settings['capacity'].items()),
        pressure_ratio=max((pressure[p]/cap for p,cap in settings['capacity'].items()),default=0),
        read_cycles=sum(graph.tensors[t]['size'] for t in reads)/bandwidth,
        operation_ids=tuple(order),
        durations=tuple(duration[v] for v in order),ddr=tuple(v in read_set or v in write_set for v in order),
        indegrees=tuple(map(len,predecessors)),successors=tuple(tuple(s) for s in following))


def simulate_plan(graph, plan, blocks, profiles, settings, *, trace=False):
    """O(operation events log active cores) fluid DDR simulation, no official calls."""
    _,pred,succ,_=quotient(graph,blocks)
    ids=sorted(set(plan['node_to_subgraph'].values()));remap={t:i for i,t in enumerate(ids)}
    core={};lags={i:{} for i in range(len(blocks))}
    for c,queue in enumerate(plan['core_schedules']):
        qq=[remap[t] for t in queue];core.update({t:c for t in qq})
        for a,b in zip(qq,qq[1:]):
            pred[b].add(a);succ[a].add(b);lags[b][a]=settings['task_same_core_wait_cycles']
    for task in pred:
        for parent in pred[task]:
            if core[parent]!=core[task]:lags[task][parent]=settings['task_cross_core_wait_cycles']
            else:lags[task].setdefault(parent,0)
    remaining=[len(pred[t]) for t in range(len(blocks))]
    release=[0]*len(blocks);left=[len(profiles[b]['durations']) for b in blocks]
    indegree={};ready=[];complete=[];starts=[];ddr={};task_ends={};now=0
    task_starts={};operation_ends=[]
    for t,n in enumerate(remaining):
        if n==0:heapq.heappush(starts,(0,t))
    def finish(t,i):
        profile=profiles[blocks[t]];left[t]-=1
        if trace and profile['operation_ids'][i] in blocks[t]:
            operation_ends.append((ids[t], profile['operation_ids'][i], now))
        for child in profile['successors'][i]:
            indegree[t][child]-=1
            if indegree[t][child]==0:ready.append((t,child))
        if left[t]==0:
            task_ends[t]=now
            for child in succ[t]:
                release[child]=max(release[child],now+lags[child][t]);remaining[child]-=1
                if remaining[child]==0:heapq.heappush(starts,(release[child],child))
    while len(task_ends)<len(blocks):
        while complete and complete[0][0]<=now:
            _,t,i=heapq.heappop(complete);finish(t,i)
        finished=[item for item,work in ddr.items() if work<=1e-8]
        for t,i in finished:del ddr[t,i];finish(t,i)
        while starts and starts[0][0]<=now:
            _,t=heapq.heappop(starts);profile=profiles[blocks[t]]
            if trace:task_starts[t]=now
            indegree[t]=list(profile['indegrees'])
            ready.extend((t,i) for i,n in enumerate(indegree[t]) if n==0)
        for t,i in ready:
            profile=profiles[blocks[t]];work=profile['durations'][i]
            if profile['ddr'][i]:ddr[t,i]=float(work)
            else:heapq.heappush(complete,(now+work,t,i))
        ready.clear()
        if len(task_ends)==len(blocks):break
        future=[]
        if complete:future.append(complete[0][0])
        if starts:future.append(starts[0][0])
        if ddr:future.append(math.ceil(now+min(ddr.values())*len(ddr)-1e-9))
        if not future:raise ValueError('approximate event model deadlock')
        nxt=min(future)
        if nxt<=now:raise ValueError('approximate clock did not advance')
        elapsed=nxt-now
        # Preserve fractional DDR work across compute and release events.
        active=[item for item,w in ddr.items() if w>1e-8]
        while elapsed>1e-9 and active:
            work=min(ddr[item] for item in active);step=work*len(active)
            share=min(elapsed,step)/len(active)
            for item in active:ddr[item]=max(0.,ddr[item]-share)
            elapsed-=min(elapsed,step)
            active=[item for item in active if ddr[item]>1e-8]
        now=nxt
    makespan=max(task_ends.values(),default=0)
    if trace:
        return dict(makespan=makespan,task_starts=task_starts,task_ends=task_ends,
                    operation_ends=operation_ends)
    return makespan
