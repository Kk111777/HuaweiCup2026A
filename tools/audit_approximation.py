"""Offline approximation audit using saved official labels; never runs official evaluation."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from q1.model import Graph
from q1.official import Settings
from q1.improvement.incremental import IncrementalEvaluator


def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))


def run_case(job):
    case,cores,data,config,base,personal,current,reference,singlecore=job
    raw=read(Path(data)/f'case_{case:03d}.json');graph=Graph(raw);setting=Settings.read(config)
    settings=dict(capacity=setting.capacity,bandwidth=setting.bandwidth,
                  task_same_core_wait_cycles=setting.same_core_wait,task_cross_core_wait_cycles=setting.cross_core_wait)
    models={'current':IncrementalEvaluator}
    if reference:
        spec=importlib.util.spec_from_file_location('q1.improvement._audit_reference',reference)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        models['reference']=mod.IncrementalEvaluator
    rows=[]
    for n in cores:
        bp=Path(base)/f'case_{case:03d}'/f'N{n}'
        pp=Path(personal)/f'case_{case:03d}'/f'N{n}'
        targets=[('base',bp/'plan.json',bp/'result.json'),
                 ('personal',pp/'personal_plan.json',pp/'personal_result.json')]
        cp=Path(current)/f'case_{case:03d}'/f'N{n}'
        if (cp/'summary.json').exists() and (cp/'evaluation.json').exists():
            targets.append(('unified',cp/'plan.json',cp/'evaluation.json'))
        base_plan,base_result=read(targets[0][1]),read(targets[0][2])
        for name,kind in models.items():
            model=kind(graph,settings,{})
            if singlecore:
                baseline = read(Path(singlecore)/f'case_{case:03d}'/f'case_{case:03d}_singlecore_res.json')
                seed = {'node_to_subgraph':{str(v):0 for v in graph.ops},
                        'core_schedules':[[0]]+[[] for _ in range(n-1)]}
                model.observe(seed,baseline)
            for origin,pf,rf in targets:
                plan,result=read(pf),read(rf)
                started=time.perf_counter();pred,meta=model.estimate(plan);elapsed=time.perf_counter()-started
                actual=result['makespan'];row=dict(case=f'case_{case:03d}',cores=n,origin=origin,model=name,
                    predicted=pred,raw_cycles=meta['raw_cycles'],actual=actual,
                    relative_error=pred/actual-1,raw_relative_error=meta['raw_cycles']/actual-1,
                    seconds=elapsed,operations=len(graph.ops),tasks=len(set(plan['node_to_subgraph'].values())),
                    calibration_samples=meta.get('calibration_samples',0),
                    pressure_risk_tasks=meta.get('pressure_risk_tasks'),
                    official_spill_bytes=result.get('data_movement_bytes',{}).get('spill_added_copy_bytes'),
                    plan=str(pf.resolve()),result=str(rf.resolve()))
                if origin!='base':
                    row.update(actual_better_than_base=actual<base_result['makespan'],
                               predicts_better_than_base=pred<base_result['makespan'])
                rows.append(row)
                # Only the already measured base plan is a calibration sample.
                # The target's own label is NEVER observed before prediction.
                if origin=='base' and not singlecore:model.observe(base_plan,base_result)
    return rows


def summarize(rows):
    stats={}
    for name in sorted({r['model'] for r in rows}):
        rr=[r for r in rows if r['model']==name];errors=sorted(abs(r['relative_error']) for r in rr)
        rank=[r for r in rr if 'actual_better_than_base' in r]
        stats[name]=dict(count=len(rr),mape=statistics.mean(errors),median_ape=statistics.median(errors),
            p90_ape=errors[min(len(errors)-1,int(.9*len(errors)))],max_ape=max(errors),
            mean_seconds=statistics.mean(r['seconds'] for r in rr),maximum_seconds=max(r['seconds'] for r in rr),
            base_comparisons=len(rank),ranking_correct=sum(r['actual_better_than_base']==r['predicts_better_than_base'] for r in rank),
            better_candidates_missed=sum(r['actual_better_than_base'] and not r['predicts_better_than_base'] for r in rank))
    return stats


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('data','config','base','personal','current','output'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--reference',type=Path)
    p.add_argument('--singlecore',type=Path,help='Use singlecore-only calibration, never observe a multicore target label')
    p.add_argument('--cases',type=int,nargs='+',default=[1,6,10,14,23,28,49,58,62,72,76,79,91,92])
    p.add_argument('--cores',type=int,nargs='+',default=[2,5])
    p.add_argument('--workers',type=int,default=4)
    a=p.parse_args();rows=[];started=time.perf_counter()
    jobs=[(c,a.cores,a.data,a.config,a.base,a.personal,a.current,a.reference,a.singlecore) for c in a.cases]
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        for rr in pool.map(run_case,jobs):
            rows.extend(rr);print(rr[0]['case'],len(rows),'predictions',flush=True)
    result=dict(stats=summarize(rows),rows=rows,elapsed_seconds=time.perf_counter()-started,
        protocol='Saved official labels; '+('singlecore-only' if a.singlecore else 'base-only')+
        ' calibration; target labels not observed; no new official evaluations.')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result['stats'],ensure_ascii=False,indent=2))


if __name__=='__main__':main()
