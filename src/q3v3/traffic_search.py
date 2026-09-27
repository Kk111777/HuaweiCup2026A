"""Q1/Q2 portfolio, capacity repair, L2 windows and guarded traffic tradeoffs."""
from collections import defaultdict
from pathlib import Path
import json
import math
import subprocess
import time

from . import ALGORITHM_NAME, ALGORITHM_REVISION
from .graph import Graph
from .state import PlanState, compute_bounds
from .official import (evaluate_file, replay, metrics, read_settings, sha256, write_json,
                       _validate_replay, ROOT)
from .traffic_objective import TrafficPolicy, select, pareto, search_beam, ddr_bytes
from .traffic_model import cache_costs, penalties
from .memory_search import MemoryService, capacity_candidates, portfolio_orders
from .cache_windows import window_candidates
from .neighborhoods import generate_candidates
from .joint_search import joint_candidates
from .global_construction import global_candidates, RegionLibrary
from .l2_structure import structural_candidates
from .q1_structure import StructureLibrary


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def cached_seed(record, graph, plan_path, config, settings):
    """Only accept a full independently verified replay of the exact inputs."""
    path=Path(record['result'])
    meta=read(record['replay'])
    if meta.get('status')!='ok' or not meta.get('complete_trace_validated'):
        raise ValueError('prepared seed is not an independently verified replay')
    for name,file in [('graph',graph),('plan',plan_path),('config',config)]:
        if sha256(file)!=meta[name+'_sha256']:
            raise ValueError(f'prepared seed {name} hash differs')
    for name,digest in meta['official_source_sha256'].items():
        if sha256(ROOT/'official/code'/name)!=digest:
            raise ValueError('prepared seed official sources differ')
    result=read(path)
    _validate_replay(result,read(path.with_name('trace.json')),
        path.with_name('official.log').read_text(encoding='utf-8'),
        Path(graph).name,Path(plan_path).name,settings)
    return result


def solve_traffic(graph_path, cores, config, output, q1_source=None, *, warm_plan=None,
                  q2_warm_plan=None, reference_plan=None, previous_plan=None,
                  prepared_seeds=None, seconds=120, max_evaluations=64,
                  evaluation_timeout=600, cycle_tolerance=.01, traffic_weight=.05,
                  added_share=.25, enable_l2=True, enable_global=True,
                  enable_joint=True, enable_structural=True, seed_policy='auto', **unused):
    if type(cores) is not int or cores not in (1,2,3,4,5):
        raise ValueError('supported core counts are 1..5')
    if (not math.isfinite(seconds) or seconds<=0 or type(max_evaluations) is not int
            or max_evaluations<3 or not math.isfinite(evaluation_timeout) or evaluation_timeout<=0):
        raise ValueError('invalid traffic-search budget')
    # Validate policy values before expensive input work.
    from .traffic_objective import rational
    for name,value in [('cycle_tolerance',cycle_tolerance),('traffic_weight',traffic_weight),('added_share',added_share)]:
        rational(value,name)
    graph_path,config,output=Path(graph_path).resolve(),Path(config).resolve(),Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    started=time.perf_counter()
    raw=read(graph_path);graph=Graph(raw);settings=read_settings(config)
    bounds=compute_bounds(graph,cores)
    input_hashes=dict(graph=sha256(graph_path),config=sha256(config))
    paths={name:Path(path).resolve() for name,path in
           [('q1',warm_plan),('q2',q2_warm_plan),('previous_q3',reference_plan)] if path is not None}
    seed_hashes={name:sha256(path) for name,path in paths.items()}
    seeds=[(name,read(path)) for name,path in paths.items()]
    if previous_plan is not None and len(previous_plan['core_schedules'])<=cores:
        seeds.append(('previous_core_count',dict(previous_plan,
            core_schedules=[list(q) for q in previous_plan['core_schedules']]+[[] for _ in range(cores-len(previous_plan['core_schedules']))])))
    if seed_policy not in ('auto','portfolio'):
        raise ValueError('seed policy must be auto or portfolio')
    bridge=None
    if not paths or seed_policy=='portfolio':
        if q1_source is None:
            raise ValueError('provide a full Q1/Q2 plan or a Q1 construction source')
        from .q1_compat import export_seeds
        bridge=export_seeds(graph_path,config,cores,Path(q1_source),output/'q1_seed',timeout=evaluation_timeout)
        seeds += [('graph_'+x['name'],x['plan']) for x in bridge['plans'][:max(0,max_evaluations-len(seeds))]]
    if len(seeds)>max_evaluations:
        raise ValueError('evaluation budget cannot validate all supplied seeds')
    history=[];seen=set();archive=[];beam=[];seed_items=[];policy=None
    best=fastest=None

    def consider(name,plan,family,reason,timeout,source=None,cache=None):
        nonlocal archive,beam,best,fastest
        try:
            state=PlanState(graph,plan)
            if len(state.queues)!=cores:
                raise ValueError('seed/candidate core count differs')
        except (ValueError,KeyError,TypeError) as exc:
            history.append(dict(name=name,family=family,status='structurally_invalid',error=str(exc)))
            write_json(output/'history.json',history)
            if family=='seed':
                raise ValueError(f'supplied seed has invalid structure: {name}: {exc}') from exc
            return None
        if state.digest in seen:
            return None
        seen.add(state.digest)
        index=sum('evaluation_directory' in r for r in history)
        folder=output/'evaluations'/f'{index:04d}'
        row=dict(name=name,family=family,reason=reason,state_digest=state.digest,
                 parent_digest=source,evaluation_directory=str(folder.relative_to(output)))
        t=time.perf_counter()
        try:
            if cache is not None:
                result=cached_seed(cache,graph_path,paths[name],config,settings)
                if PlanState(graph,read(paths[name])).digest!=state.digest:
                    raise ValueError('cached input contract differs')
                folder.mkdir(parents=True,exist_ok=False)
                write_json(folder/'plan.json',state.plan)
                write_json(folder/'result.json',result)
                write_json(folder/'evaluation.json',dict(status='ok',source='validated prepared seed',
                    source_result=cache['result'],source_result_sha256=sha256(cache['result'])))
            else:
                result=evaluate_file(graph_path,state.plan,config,folder,timeout=timeout)
            cost=cache_costs(result)
            row.update(status='ok',**metrics(result),fifo_reread_bytes=cost['bytes'].get('fifo',0),
                       inflight_miss_bytes=cost['bytes'].get('inflight',0),penalties=penalties(result,cost))
            item=dict(name=name,plan=state.plan,result=result,digest=state.digest,
                      evaluation_directory=row['evaluation_directory'])
            archive=pareto(archive+[item])
            if policy is None:
                best=fastest=min(archive,key=lambda x:(x['result']['makespan'],ddr_bytes(x['result']),x['digest']))
            else:
                old=best['digest']
                best,fastest=select(archive,policy)
                beam=search_beam(beam+archive+[item],policy)
                row.update(accepted=best['digest']!=old,selected_digest=best['digest'],
                           objective=policy.record(result,fastest['result']['makespan']))
                write_json(output/'checkpoint.json',dict(plan=best['plan'],name=best['name'],
                    objective=policy.record(best['result'],fastest['result']['makespan'])))
            return item
        except (ValueError,OSError,RuntimeError,TimeoutError,subprocess.TimeoutExpired) as exc:
            row.update(status='official_rejected',error=str(exc),
                       timed_out=isinstance(exc,(TimeoutError,subprocess.TimeoutExpired)))
            if family=='seed':
                raise ValueError(f'supplied seed failed Q3 validation: {name}: {exc}') from exc
            return None
        finally:
            row['seconds']=time.perf_counter()-t
            history.append(row)
            write_json(output/'history.json',history)

    for name,plan in seeds:
        item=consider(name,plan,'seed','Complete independently Q3-evaluated solution; no cached Q1/Q2 score.',
                      evaluation_timeout,cache=(prepared_seeds or {}).get(name))
        if item is not None:
            seed_items.append(item)
        else:
            digest=PlanState(graph,plan).digest
            matching=next((x for x in seed_items if x['digest']==digest),None)
            if matching is not None:
                seed_items.append(dict(matching,name=name))
    if best is None:
        raise RuntimeError('no valid complete seed')
    normalizer=min(seed_items,key=lambda x:(x['result']['makespan'],ddr_bytes(x['result'])))
    policy=TrafficPolicy.from_result(normalizer['result'],tolerance=cycle_tolerance,
                                    weight=traffic_weight,added_share=added_share)
    best,fastest=select(archive,policy)
    seed_best=best
    beam=search_beam(archive,policy)
    write_json(output/'policy.json',policy.record(best['result'],fastest['result']['makespan']))
    write_json(output/'seed_results.json',[dict(name=x['name'],metrics=metrics(x['result']),digest=x['digest'],
        objective=policy.record(x['result'],fastest['result']['makespan'])) for x in seed_items])
    preparation_seconds=time.perf_counter()-started
    search_started=time.perf_counter()
    def check():
        if time.perf_counter()-search_started>=seconds:
            raise TimeoutError('proposal budget exhausted')
    memory=MemoryService(graph,settings)
    library=RegionLibrary(graph,cores,settings)
    structures=StructureLibrary(graph,cores) if enable_structural else None
    streams={};done=set();expansions=[];family_turn=defaultdict(int)
    portfolio=[(x['name'],x['plan']) for x in sorted(seed_items,key=lambda x:(
        x['result']['data_movement_bytes']['spill_added_copy_bytes'],ddr_bytes(x['result']),x['name']))]

    def memory_stream(item):
        yield from portfolio_orders(graph,item['plan'],portfolio)
        yield from capacity_candidates(graph,item['plan'],item['result'],settings,memory,check)

    def next_proposal(family):
        origins=[]
        for x in [best,fastest]+beam:
            if all(x['digest']!=old['digest'] for old in origins): origins.append(x)
        # Persist each generator. Rotate origins so a new incumbent does not
        # repeatedly discard partially searched capacity windows.
        start=family_turn[family]%max(1,len(origins));family_turn[family]+=1
        for item in origins[start:]+origins[:start]:
            token=(family,item['digest'])
            if token in done: continue
            if token not in streams:
                p,r=item['plan'],item['result']
                if family=='memory': gen=memory_stream(item)
                elif family=='cache': gen=window_candidates(graph,p,r,settings)
                elif family=='joint': gen=joint_candidates(graph,p,r,settings,library,check)
                elif family=='structural': gen=structural_candidates(graph,p,r,settings,structures,1,check,enable_l2=enable_l2)
                else: gen=generate_candidates(graph,p,r,settings,max_candidates=16,enable_l2=enable_l2)
                streams[token]=iter(gen)
                expansions.append(dict(family=family,parent_digest=item['digest'],calls=len(history)))
            try:
                return next(streams[token]),item['digest']
            except StopIteration:
                done.add(token);del streams[token]
        return None

    global_stream=iter(global_candidates(graph,cores,settings,library,check)) if enable_global else None
    slots=['memory','memory','local','memory']
    if enable_l2: slots+=['cache']
    if enable_joint: slots+=['joint']
    if enable_structural: slots+=['structural']
    if enable_global: slots+=['global']
    if enable_l2: slots+=['cache']
    cursor=0;stop='candidate_exhausted'
    while True:
        calls=sum('evaluation_directory' in r for r in history)
        if calls>=max_evaluations: stop='official_call_budget';break
        if time.perf_counter()-search_started>=seconds: stop='search_time_budget';break
        proposal=None
        try:
            for _ in slots:
                family=slots[cursor%len(slots)];cursor+=1
                check()
                if family=='global':
                    if global_stream is None: continue
                    try: proposal=(next(global_stream),None)
                    except StopIteration: global_stream=None
                else:
                    proposal=next_proposal(family)
                if proposal is not None: break
        except TimeoutError:
            stop='search_time_budget';break
        if proposal is None: break
        candidate,parent=proposal
        remaining=seconds-(time.perf_counter()-search_started)
        if remaining<=.1: stop='search_time_budget';break
        consider(candidate.family,candidate.plan,candidate.family,candidate.reason,
                 min(evaluation_timeout,remaining),parent)
    search_seconds=time.perf_counter()-search_started
    best,fastest=select(archive,policy)
    write_json(output/'plan.json',best['plan'])
    write_json(output/'fastest/plan.json',fastest['plan'])
    t=time.perf_counter()
    verified=replay(graph_path,output/'plan.json',config,output/'verified',timeout=evaluation_timeout)
    fast_verified=replay(graph_path,output/'fastest/plan.json',config,output/'fastest/verified',timeout=evaluation_timeout)
    final_seconds=time.perf_counter()-t
    if metrics(verified)!=metrics(best['result']) or metrics(fast_verified)!=metrics(fastest['result']):
        raise RuntimeError('final official replay differs from search')
    if not policy.eligible(verified,fast_verified['makespan']):
        raise RuntimeError('delivered plan violates the cycle guard')
    if input_hashes!=dict(graph=sha256(graph_path),config=sha256(config)):
        raise RuntimeError('input/config changed during search')
    if seed_hashes!={name:sha256(path) for name,path in paths.items()}:
        raise RuntimeError('input seed changed during search')
    write_json(output/'memory_profiles.json',memory.diagnostics)
    write_json(output/'expansions.json',expansions)
    write_json(output/'bounds.json',bounds)
    write_json(output/'pareto.json',[dict(name=x['name'],digest=x['digest'],metrics=metrics(x['result']),
        evaluation_directory=x['evaluation_directory'],objective=policy.record(x['result'],fastest['result']['makespan'])) for x in archive])
    write_json(output/'cache_analysis.json',dict(final=cache_costs(verified),fastest=cache_costs(fast_verified)))
    summary=dict(algorithm=ALGORITHM_NAME,algorithm_revision=ALGORITHM_REVISION,
        case=graph_path.stem,status='ok',**metrics(verified),winner=best['name'],
        objective_policy='cycles-traffic',objective=policy.record(verified,fast_verified['makespan']),
        fastest_metrics=metrics(fast_verified),fastest_name=fastest['name'],
        slowdown_from_fastest_percent=100*(verified['makespan']/max(1,fast_verified['makespan'])-1),
        seed_name=seed_best['name'],seed_makespan=seed_best['result']['makespan'],
        seed_metrics=metrics(seed_best['result']),
        supplied_plan_metrics=metrics(next(x for x in seed_items if x['name']=='q1')['result']) if any(x['name']=='q1' for x in seed_items) else None,
        seed_results={x['name']:metrics(x['result']) for x in seed_items},
        reduction_from_seed_percent=100*(1-verified['makespan']/max(1,seed_best['result']['makespan'])),
        source_hashes=input_hashes,seed_hashes=seed_hashes,seed_paths={k:str(v) for k,v in paths.items()},
        plan_sha256=sha256(output/'plan.json'),fastest_plan_sha256=sha256(output/'fastest/plan.json'),
        warm_start_mode='graph_seed_portfolio' if bridge else 'complete_solution_portfolio',
        singlecore_score_used=False,q1_source_provenance=bridge['provenance'] if bridge else None,
        enable_l2=enable_l2,enable_global=enable_global,enable_joint=enable_joint,
        enable_structural=enable_structural,lower_bound=bounds['lower_bound'],
        official_calls=sum('evaluation_directory' in r for r in history),
        rejected_calls=sum(r['status']=='official_rejected' for r in history),
        preparation_seconds=preparation_seconds,search_seconds=search_seconds,final_replay_seconds=final_seconds,
        total_seconds=time.perf_counter()-started,stop_reason=stop,
        memory_calls=sum(r['family'].startswith(('memory_','capacity_')) for r in history),
        verification='independent official Q3 CLI for both selected and fastest plans')
    write_json(output/'summary.json',summary)
    return summary,best['plan']
